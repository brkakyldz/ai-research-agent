"""Entry points for a run.

Two of them, and they are the only two ways work starts: the three-hourly
collect, and the digest - which a person starts from `/runs` (ADR 0015) or the
CLI. Those two words are also the two values of `Run.kind` (ADR 0026): a run is
the free poll or it is the one that spends money, and nothing else distinguishes
them. The delta logic lives in candidate selection rather than here, so an
article already summarised is never a candidate again and pressing the button
twice costs nothing twice.

Both take the one run slot before they open anything and hold it until they are
done, so a terminal digest and a pressed one cannot overlap however they were
started. That is the whole concurrency story and it is enforced here rather than
asked of the caller.

Each opens a run row before the work and closes it afterwards, including on
failure. A run that crashed and left `status='running'` forever would be the one
thing the /runs page could not explain.

A failed digest is picked up by pressing again. Each summarise branch commits
its own row (ADR 0030), so the work a dead run bought is in the archive and not
in a checkpoint file: `_unsummarized` skips those articles and the next press
ranks them. That is why there is no resume - the recovery is the ordinary path,
and it does not depend on a checkpoint whose prompts, models and state schema
have all moved on since it was written (ADR 0032).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.clock import local_day
from ainews.config import Language, Settings, get_settings
from ainews.db import Bulletin, Run
from ainews.db.models import utcnow
from ainews.db.session import session_scope
from ainews.pipeline.graph import build_graph
from ainews.pipeline.nodes.collect import collect_articles
from ainews.pipeline.pricing import resolve_model
from ainews.pipeline.state import PipelineState
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


async def resolve_bulletin(session: AsyncSession, ref: str) -> int:
    """`latest`, a numeric id, a `YYYY-MM-DD`, or a `YYYY-MM-DD:tr`.

    What every `ainews eval` subcommand takes, because a measurement is about a
    published bulletin rather than about the press that paid for it (ADR 0030).
    A date is the form a person actually has in mind, and it resolves to that
    day's current version - the page they would be looking at.
    """
    query = select(Bulletin.id).order_by(
        Bulletin.day.desc(), Bulletin.version.desc(), Bulletin.id.desc()
    )
    if ref != "latest":
        day, _, language = ref.partition(":")
        if day.isdigit():
            found = await session.get(Bulletin, int(day))
            if found is None:
                raise LookupError(f"no bulletin {day}")
            return found.id
        query = query.where(Bulletin.day == day)
        if language:
            query = query.where(Bulletin.language == language)
    bulletin_id = (await session.execute(query.limit(1))).scalars().first()
    if bulletin_id is None:
        raise LookupError(f"no bulletin matches {ref!r}")
    return bulletin_id


async def _open_run(
    kind: str,
    language: str,
    model_summarize: str | None = None,
    model_rank: str | None = None,
) -> str:
    async with session_scope() as session:
        run = Run(
            kind=kind,
            language=language,
            status="running",
            model_summarize=model_summarize,
            model_rank=model_rank,
        )
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


async def reconcile_orphaned_runs() -> int:
    """Close run rows that a killed process left open. Returns how many.

    `_fail_run` closes a run that *raised*. A process that is killed does not
    raise: a closed laptop, `docker compose down`, Ctrl-C at a terminal. The row
    keeps `status='running'` and `finished_at` NULL forever, the run log shows a
    poll that has been going for a day, and the run slot's own advice block reads
    a state the machine is not in.

    Called from the web application's startup, which is the moment the claim
    below is true: the run slot is module state (`_slot_holders`), so a process
    that has just started holds nothing, and one process owns the runs (ADR
    0004). A digest running at a terminal while the server restarts would be
    closed here while it is still working - the price of a single-user tool
    having two entry points, and cheaper than a row that nothing can ever close.
    """
    async with session_scope() as session:
        rows = list((await session.execute(select(Run).where(Run.status == "running"))).scalars())
        for run in rows:
            run.status = "error"
            run.error = "process ended before the run did"
            run.finished_at = utcnow()
    if rows:
        log.warning("closed %d run(s) left open by a previous process", len(rows))
    return len(rows)


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


async def run_digest(
    language: Language | None = None,
    settings: Settings | None = None,
    model_summarize: str | None = None,
    model_rank: str | None = None,
    reserved_token: str | None = None,
) -> str:
    """The full pipeline. Returns the run id.

    Holds the one run slot for its whole length and raises `RunBusy` when
    another run has it, so a terminal digest and a pressed one cannot select the
    same candidates and pay for them twice. `reserved_token` is for the caller
    that had to take the slot before scheduling this - `reserve_slot` says why.

    The two model arguments have the same standing as `language` (ADR 0020):
    the caller decides, the environment is only the default, and the choice is
    resolved once here so every node downstream reads a name rather than a
    setting. Resolution happens before the run row opens, so the line the log
    prints is the line the bill will match.
    """
    settings = settings or get_settings()
    token = reserved_token or f"digest:{uuid.uuid4().hex[:8]}"
    async with _slot(token, "digest", reserved_token is not None):
        return await _start_digest(language, settings, model_summarize, model_rank)


async def _start_digest(
    language: Language | None,
    settings: Settings,
    model_summarize: str | None,
    model_rank: str | None,
) -> str:
    language = language or settings.digest_language
    model_summarize = resolve_model(model_summarize, settings.openai_model_summarize)
    model_rank = resolve_model(model_rank, settings.openai_model)
    # The models are on the row from the start, not inferred from `run_steps`
    # afterwards: a run that fails at `collect` writes no paid step at all, and
    # what was bought is a fact about the press rather than about what happened.
    run_id = await _open_run("digest", language, model_summarize, model_rank)
    log.info(
        "run %s starting (language=%s, summarize=%s, rank=%s)",
        run_id,
        language,
        model_summarize,
        model_rank,
    )

    initial: PipelineState = {
        "run_id": run_id,
        "language": language,
        # The reader's calendar day, fixed when the run opens and carried in
        # the state, so a run that starts at 23:59 and finishes at 00:04
        # publishes one day rather than two.
        "day": local_day(),
        "model_summarize": model_summarize,
        "model_rank": model_rank,
        "candidate_ids": [],
        "summaries": [],
        "ranked": [],
        "errors": [],
    }
    await _invoke(run_id, initial, settings)
    return run_id


async def _invoke(run_id: str, initial: PipelineState, settings: Settings) -> None:
    try:
        app = build_graph().compile()
        await app.ainvoke(
            initial,
            config={
                "recursion_limit": RECURSION_LIMIT,
                # Keeps a hundred branches from becoming a hundred simultaneous
                # requests and a wall of 429s.
                "max_concurrency": settings.summarize_batch_size,
            },
        )
    except Exception as exc:
        log.exception("digest run %s failed", run_id)
        await _fail_run(run_id, f"{type(exc).__name__}: {exc}")
        raise
