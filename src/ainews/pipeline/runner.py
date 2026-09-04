"""Entry points for a run.

Three of them, and they are the only three ways work starts: the three-hourly
collect, the daily digest, and the "Run now" button - which is the digest with a
different `kind` on the run row, because the delta logic lives in candidate
selection rather than here (an article already summarised is never a candidate
again, so pressing the button twice costs nothing twice).

Each opens a run row before the work and closes it afterwards, including on
failure. A run that crashed and left `status='running'` forever would be the one
thing the /runs page could not explain.
"""

from __future__ import annotations

import logging

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from ainews.config import Language, Settings, get_settings
from ainews.db import Run
from ainews.db.models import utcnow
from ainews.db.session import session_scope
from ainews.pipeline.graph import build_graph, checkpoint_path
from ainews.pipeline.nodes.collect import collect_articles
from ainews.pipeline.state import PipelineState, RunMode

log = logging.getLogger(__name__)

# A `Send` fan-out is one superstep however wide it is, so the real depth here
# is six. The limit is set far above that anyway: it is free insurance against a
# future node that loops, and the default of 25 is not obviously above six.
RECURSION_LIMIT = 200


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
        async with session_scope() as session:
            stats = await collect_articles(session, settings)
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
) -> str:
    """The full pipeline. Returns the run id, which is also the checkpoint thread."""
    settings = settings or get_settings()
    language = language or settings.digest_language
    kind = "digest" if mode == "digest" else "manual"
    run_id = await _open_run(kind, language)
    log.info("run %s starting (%s, language=%s)", run_id, kind, language)

    initial: PipelineState = {
        "run_id": run_id,
        "language": language,
        "mode": mode,
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
