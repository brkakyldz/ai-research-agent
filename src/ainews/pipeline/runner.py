"""Entry points for a run.

Two of them, and they are the only two ways work starts: the three-hourly
collect, and the digest - which a person starts from `/runs` (ADR 0015) or the
CLI, with `manual` or `digest` written on the run row. The delta logic lives in
candidate selection rather than here, so an article already summarised is never
a candidate again and pressing the button twice costs nothing twice.

Both take the one run slot before they open anything and hold it until they are
done, so a terminal digest and a pressed one cannot overlap however they were
started. That is the whole concurrency story and it is enforced here rather than
asked of the caller.

Each opens a run row before the work and closes it afterwards, including on
failure. A run that crashed and left `status='running'` forever would be the one
thing the /runs page could not explain.

A failed digest can be picked up where it stopped. The graph checkpoints every
superstep into `checkpoints.db` under the run's id, and until 2026-09-08 nothing
ever read those checkpoints back: a run that died at `persist` had a hundred paid
summaries sitting in the checkpoint file, and the next press summarised the same
hundred articles again, because `_unsummarized` saw no `Summary` rows. `resume`
is the read: `ainvoke(None, thread_id=run_id)` re-runs the node that failed and
everything after it, and nothing before it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Language, Settings, get_settings
from ainews.db import Run
from ainews.db.models import utcnow
from ainews.db.session import session_scope
from ainews.pipeline.graph import build_graph, checkpoint_path
from ainews.pipeline.llm import resolve_model
from ainews.pipeline.nodes.collect import collect_articles
from ainews.pipeline.state import PipelineState, RunMode
from ainews.pipeline.steps import step

log = logging.getLogger(__name__)

# A `Send` fan-out is one superstep however wide it is, so the real depth here
# is six. The limit is set far above that anyway: it is free insurance against a
# future node that loops, and the default of 25 is not obviously above six.
RECURSION_LIMIT = 200


class RunBusy(RuntimeError):
    """Another run holds the slot. Raised by `run_digest` and `run_collect`."""


# One run at a time, whatever started it. SQLite has a single writer (ADR 0003)
# and everything shares this process (ADR 0004), so a module-level claim is the
# whole concurrency story - but only if every path takes it, and the way to make
# that true is for the slot to be taken by the two functions that *are* the entry
# points rather than by their callers. It used to be the caller's job, documented
# as "only if every path takes it", and the CLI did not: `ainews digest` in a
# terminal alongside a press on `/runs` selected the same unsummarised candidates
# and paid the model twice for one day.
#
# One slot and not two, because a collect and a digest race as well: a digest's
# first node is the same feed poll, and `_insert_new_items` checks then inserts,
# so an overlapping scheduled poll turns into an IntegrityError that fails one of
# the two runs.
_slot_lock = asyncio.Lock()
_slot_holders: dict[str, str] = {}  # token -> kind


def digest_in_flight() -> bool:
    """A *paid* run is in flight. The three-hourly poll holds the same slot but
    is not what the button and the advice block are reporting on."""
    return any(kind != "collect" for kind in _slot_holders.values())


def run_in_flight() -> bool:
    return bool(_slot_holders)


async def reserve_slot(token: str, kind: str) -> bool:
    """Take the slot ahead of the work, or report that someone else holds it.

    For the one caller that cannot let `run_digest` do it: `/runs/start` answers
    the request before the run finishes, so it schedules a task and returns.
    `create_task` only schedules - a second press arriving before the task's
    first line would find the slot free and start a second, paid, digest. So the
    route reserves, then hands the token to `run_digest`, which holds and
    releases it exactly as if it had taken it itself.
    """
    async with _slot_lock:
        if _slot_holders:
            return False
        _slot_holders[token] = kind
        return True


def release_slot(token: str) -> None:
    """Idempotent: a reserving caller may also release in its own `finally`, for
    the case where the task it scheduled never ran at all."""
    _slot_holders.pop(token, None)


@asynccontextmanager
async def _slot(token: str, kind: str, reserved: bool) -> AsyncIterator[None]:
    if not reserved and not await reserve_slot(token, kind):
        raise RunBusy(f"a {next(iter(_slot_holders.values()))} run is already in flight")
    try:
        yield
    finally:
        release_slot(token)


async def resolve_run_id(session: AsyncSession, ref: str) -> str:
    """`latest`, a full id, or an unambiguous prefix of one.

    Shared by `ainews digest --resume` and every `ainews eval` subcommand; a
    person at a terminal types the first six characters of a uuid, not thirty-two.
    """
    if ref == "latest":
        run = (
            await session.execute(
                select(Run)
                .where(Run.kind != "collect")
                .where(Run.n_summarized > 0)
                .order_by(Run.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if run is None:
            raise LookupError("no digest run with summaries in the database")
        return run.id
    rows = list((await session.execute(select(Run.id).where(Run.id.like(f"{ref}%")))).scalars())
    if not rows:
        raise LookupError(f"no run starts with {ref!r}")
    if len(rows) > 1:
        raise LookupError(f"{ref!r} is ambiguous: {', '.join(rows)}")
    return rows[0]


async def _open_run(kind: str, language: str) -> str:
    async with session_scope() as session:
        run = Run(kind=kind, language=language, status="running")
        session.add(run)
        await session.flush()
        return run.id


async def _fail_run(run_id: str, error: str) -> None:
    async with session_scope() as session:
        run = await session.get(Run, run_id)
        if run is None:
            return
        run.status = "error"
        run.finished_at = utcnow()
        run.error = error[:2000]


async def run_collect(
    settings: Settings | None = None, *, reserved_token: str | None = None
) -> str:
    """The three-hourly poll. No LLM, no cost, no digest.

    Raises `RunBusy` rather than queueing when a digest is in flight: the poll
    comes round again in three hours, and the digest's own first node is the
    same poll, so nothing is missed by standing aside.
    """
    settings = settings or get_settings()
    token = reserved_token or f"collect:{uuid.uuid4().hex[:8]}"
    async with _slot(token, "collect", reserved_token is not None):
        return await _collect(settings)


async def _collect(settings: Settings) -> str:
    run_id = await _open_run("collect", settings.digest_language)
    try:
        # A collect run is one node, so it gets one `run_steps` row (ADR 0022).
        # It never enters the graph, so the row is written here rather than by an
        # adapter - without it the run detail page would show a run with no
        # steps, which reads as a bug rather than as a poll.
        async with step(run_id, "collect") as marker:
            async with session_scope() as session:
                stats = await collect_articles(session, settings)
            marker.counts(stats.n_seen, stats.n_new)
            marker.note_key("collect", sources=stats.n_sources, unchanged=stats.n_not_modified)
            if stats.errors:
                marker.status = "partial"
                marker.note("; ".join(stats.errors))
        async with session_scope() as session:
            run = await session.get(Run, run_id)
            run.finished_at = utcnow()
            run.n_collected = stats.n_seen
            run.n_new = stats.n_new
            run.error = "\n".join(stats.errors or []) or None
            run.status = "partial" if stats.errors else "ok"
    except Exception as exc:
        log.exception("collect run %s failed", run_id)
        await _fail_run(run_id, f"{type(exc).__name__}: {exc}")
        raise
    return run_id


@dataclass(slots=True)
class Resumable:
    """What a failed run's checkpoint says is left to do."""

    run: Run
    # The node the graph will start from - the one that raised.
    next_node: str


async def pending_nodes(run_id: str, settings: Settings | None = None) -> list[str]:
    """The nodes a run's checkpoint has still to execute; empty when there is
    nothing to resume - no checkpoint under that id, or a graph that ran to
    `END`. Read-only: opens the checkpoint file and closes it."""
    settings = settings or get_settings()
    checkpoints = checkpoint_path(settings)
    if not checkpoints.is_file():
        return []
    async with AsyncSqliteSaver.from_conn_string(str(checkpoints)) as saver:
        app = build_graph().compile(checkpointer=saver)
        snapshot = await app.aget_state({"configurable": {"thread_id": run_id}})
    return list(snapshot.next or ())


async def resumable_run(
    session: AsyncSession, settings: Settings | None = None
) -> Resumable | None:
    """The most recent digest, if it failed and its checkpoint can carry on.

    Only the most recent: an older failure has been overtaken by a run that
    summarised the same candidates, so resuming it would write a second bulletin
    for a day that already has one.
    """
    last = (
        await session.execute(
            select(Run)
            .where(Run.kind != "collect")
            .where(Run.status != "running")
            .order_by(Run.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is None or last.status != "error":
        return None
    pending = await pending_nodes(last.id, settings)
    return Resumable(run=last, next_node=pending[0]) if pending else None


async def run_digest(
    language: Language | None = None,
    mode: RunMode = "digest",
    settings: Settings | None = None,
    model_summarize: str | None = None,
    model_rank: str | None = None,
    resume: str | None = None,
    reserved_token: str | None = None,
) -> str:
    """The full pipeline. Returns the run id, which is also the checkpoint thread.

    Holds the one run slot for its whole length and raises `RunBusy` when
    another run has it, so a terminal digest and a pressed one cannot select the
    same candidates and pay for them twice. `reserved_token` is for the caller
    that had to take the slot before scheduling this - `reserve_slot` says why.

    The two model arguments have the same standing as `language` (ADR 0020):
    the caller decides, the environment is only the default, and the choice is
    resolved once here so every node downstream reads a name rather than a
    setting. Resolution happens before the run row opens, so the line the log
    prints is the line the bill will match.

    With `resume` the other arguments are ignored: the language and the models
    are in the checkpoint, and a run finished by a different model from the one
    that started it would be priced as neither. The run row is reopened rather
    than a new one written, so the bulletin lands on the id the reader saw fail.
    """
    settings = settings or get_settings()
    token = reserved_token or f"digest:{resume or uuid.uuid4().hex[:8]}"
    async with _slot(token, "digest", reserved_token is not None):
        if resume is not None:
            return await _resume_digest(resume, settings)
        return await _start_digest(language, mode, settings, model_summarize, model_rank)


async def _start_digest(
    language: Language | None,
    mode: RunMode,
    settings: Settings,
    model_summarize: str | None,
    model_rank: str | None,
) -> str:
    language = language or settings.digest_language
    model_summarize = resolve_model(model_summarize, settings.openai_model_summarize)
    model_rank = resolve_model(model_rank, settings.openai_model)
    kind = "digest" if mode == "digest" else "manual"
    run_id = await _open_run(kind, language)
    log.info(
        "run %s starting (%s, language=%s, summarize=%s, rank=%s)",
        run_id,
        kind,
        language,
        model_summarize,
        model_rank,
    )

    initial: PipelineState = {
        "run_id": run_id,
        "language": language,
        "model_summarize": model_summarize,
        "model_rank": model_rank,
        "candidate_ids": [],
        "summaries": [],
        "ranked": [],
        "errors": [],
    }
    await _invoke(run_id, initial, settings)
    return run_id


async def _resume_digest(run_id: str, settings: Settings) -> str:
    async with session_scope() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise LookupError(f"no run {run_id}")
        if run.kind == "collect":
            raise ValueError(f"run {run_id} is a feed poll; only a digest can be resumed")
        if run.status != "error":
            raise ValueError(f"run {run_id} is {run.status}; only a failed run can be resumed")
    pending = await pending_nodes(run_id, settings)
    if not pending:
        raise LookupError(f"run {run_id} has no checkpoint to resume from")

    log.info("run %s resuming at %s", run_id, pending[0])
    async with session_scope() as session:
        run = await session.get(Run, run_id)
        run.status = "running"
        run.finished_at = None
        run.error = None
    # `None` as the input is LangGraph's "carry on from the checkpoint": the
    # state is the one the last completed superstep wrote, and execution starts
    # at the node that was next.
    await _invoke(run_id, None, settings)
    return run_id


async def _invoke(run_id: str, initial: PipelineState | None, settings: Settings) -> None:
    try:
        checkpoints = checkpoint_path(settings)
        checkpoints.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(checkpoints)) as saver:
            app = build_graph().compile(checkpointer=saver)
            await app.ainvoke(
                initial,
                config={
                    "configurable": {"thread_id": run_id},
                    "recursion_limit": RECURSION_LIMIT,
                    # Keeps a hundred branches from becoming a hundred
                    # simultaneous requests and a wall of 429s.
                    "max_concurrency": settings.summarize_batch_size,
                },
                # Written before the next superstep starts, not alongside it.
                # The default lets a checkpoint lag one step behind the graph,
                # which is exactly the step a resume needs to have been kept.
                durability="sync",
            )
    except Exception as exc:
        log.exception("digest run %s failed", run_id)
        await _fail_run(run_id, f"{type(exc).__name__}: {exc}")
        raise
