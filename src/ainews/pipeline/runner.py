"""Entry points for a run.

Two of them, and they are the only two ways work starts: the three-hourly
collect, and the digest - which a person starts from `/runs` (ADR 0015) or the
CLI, with `manual` or `digest` written on the run row. The delta logic lives in
candidate selection rather than here, so an article already summarised is never
a candidate again and pressing the button twice costs nothing twice.

Each opens a run row before the work and closes it afterwards, including on
failure. A run that crashed and left `status='running'` forever would be the one
thing the /runs page could not explain.
"""

from __future__ import annotations

import asyncio
import logging

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

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

# One digest at a time, whatever started it. SQLite has a single writer (ADR 0003)
# and everything shares this process (ADR 0004), so a module-level claim is the
# whole concurrency story - but only if every path takes it. It lives here rather
# than in the web layer because the CLI never goes through a route, and two
# digests over the same candidates means paying the model twice for one day.
_digest_lock = asyncio.Lock()
_digest_running: set[str] = set()


def digest_in_flight() -> bool:
    return bool(_digest_running)


async def try_claim_digest(token: str) -> bool:
    """Take the digest slot, or report that someone else holds it."""
    async with _digest_lock:
        if _digest_running:
            return False
        _digest_running.add(token)
        return True


def release_digest(token: str) -> None:
    _digest_running.discard(token)


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


async def run_collect(settings: Settings | None = None) -> str:
    """The three-hourly poll. No LLM, no cost, no digest."""
    settings = settings or get_settings()
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
    mode: RunMode = "digest",
    settings: Settings | None = None,
    model_summarize: str | None = None,
    model_rank: str | None = None,
) -> str:
    """The full pipeline. Returns the run id, which is also the checkpoint thread.

    The two model arguments have the same standing as `language` (ADR 0020):
    the caller decides, the environment is only the default, and the choice is
    resolved once here so every node downstream reads a name rather than a
    setting. Resolution happens before the run row opens, so the line the log
    prints is the line the bill will match.
    """
    settings = settings or get_settings()
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
        "mode": mode,
        "model_summarize": model_summarize,
        "model_rank": model_rank,
        "candidate_ids": [],
        "summaries": [],
        "ranked": [],
        "errors": [],
    }

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
            )
    except Exception as exc:
        log.exception("digest run %s failed", run_id)
        await _fail_run(run_id, f"{type(exc).__name__}: {exc}")
        raise

    return run_id
