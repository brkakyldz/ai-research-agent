"""What a local tool is allowed to forget.

The archive is the point of this thing and nothing here touches it: a summary,
a run row, a verdict and a judge's finding are all kept forever, and the front
page, `/archive`, the search index and every evaluation number are read off
them. Two other things accumulate that no reader ever reaches.

**Checkpoint threads.** The graph writes a checkpoint after every superstep, at
`durability="sync"`, one row per fan-out branch — so a ninety-story run leaves
about two hundred rows in `checkpoints.db`. They exist for one purpose, which is
`ainews digest --resume`, and `_resume_digest` refuses any run that is not in
`error`. A thread belonging to a run that finished can therefore never be read
again, and a thread whose run row has gone cannot even be addressed. Five runs
had already left 1,053 write rows and 1.5 MB.

**Articles nobody will ever summarise.** `nodes/dedupe._unsummarized` only
considers articles inside `collect_max_age_days`, so an article past that horizon
with no summary on it is not a candidate now and cannot become one later. It is
a row the collect poll wrote and the pipeline stepped over.

Nothing here is scheduled, for the same reason nothing schedules a digest
(ADR 0015): a tool that deletes on a timer is a tool that deletes while you are
not looking. It is a command, it prints what it would do with `--dry-run`, and
it refuses to run at all while a run is in flight.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select, text, update

from ainews.config import Settings, get_settings
from ainews.db import Article, Run, Summary
from ainews.db.models import utcnow
from ainews.db.session import session_scope
from ainews.pipeline.graph import checkpoint_path
from ainews.pipeline.runner import RunBusy, run_in_flight

log = logging.getLogger(__name__)


@dataclass(slots=True)
class PruneReport:
    """What was removed, or would be. Every number is counted before the delete,
    so `--dry-run` and the real thing report the same figures."""

    threads: int
    checkpoint_rows: int
    articles: int
    checkpoints_bytes: int
    database_bytes: int
    dry_run: bool

    @property
    def total_bytes(self) -> int:
        return self.checkpoints_bytes + self.database_bytes


async def prune(settings: Settings | None = None, *, dry_run: bool = False) -> PruneReport:
    """Drop the checkpoint threads and dead articles nothing can reach.

    Refuses while a run holds the slot: a digest in flight is writing to the
    very checkpoint thread this would delete, and the run row it would be
    matched against does not have its final status yet.
    """
    settings = settings or get_settings()
    if run_in_flight():
        raise RunBusy("a run is in flight; prune when it has finished")

    threads, rows = await _prune_checkpoints(settings, dry_run=dry_run)
    articles = await _prune_articles(settings, dry_run=dry_run)
    return PruneReport(
        threads=threads,
        checkpoint_rows=rows,
        articles=articles,
        checkpoints_bytes=await _reclaim_checkpoints(settings, dry_run=dry_run),
        database_bytes=await _reclaim_database(dry_run=dry_run),
        dry_run=dry_run,
    )


async def _resumable_threads() -> set[str]:
    """The thread ids a resume could still address: failed runs, and only those.

    Read from `runs` rather than from the checkpoint file, because the question
    is not "what threads exist" but "what could still be asked for".
    """
    async with session_scope() as session:
        return set((await session.execute(select(Run.id).where(Run.status == "error"))).scalars())


async def _prune_checkpoints(settings: Settings, *, dry_run: bool) -> tuple[int, int]:
    path = checkpoint_path(settings)
    if not path.is_file():
        return 0, 0
    keep = await _resumable_threads()

    # A plain connection: LangGraph owns this file's schema and opens it with its
    # own saver, so there is no engine or model to go through. Two tables, both
    # keyed by `thread_id`.
    with sqlite3.connect(path) as conn:
        threads = {row[0] for row in conn.execute("SELECT DISTINCT thread_id FROM checkpoints")}
        doomed = threads - keep
        if not doomed:
            return 0, 0
        marks = ",".join("?" * len(doomed))
        args = tuple(doomed)
        rows = sum(
            conn.execute(
                f"SELECT count(*) FROM {table} WHERE thread_id IN ({marks})", args
            ).fetchone()[0]
            for table in ("checkpoints", "writes")
        )
        if not dry_run:
            for table in ("checkpoints", "writes"):
                conn.execute(f"DELETE FROM {table} WHERE thread_id IN ({marks})", args)
            conn.commit()
    return len(doomed), rows


async def _prune_articles(settings: Settings, *, dry_run: bool) -> int:
    horizon = utcnow() - timedelta(days=settings.collect_max_age_days)
    dead = (
        select(Article.id)
        .where(Article.fetched_at < horizon)
        .where(~select(Summary.id).where(Summary.article_id == Article.id).exists())
    )
    async with session_scope() as session:
        ids = list((await session.execute(dead)).scalars())
        if not ids or dry_run:
            return len(ids)
        # A surviving article may be marked a duplicate *of* one that is going.
        # The mark is inspectable history, not a foreign key worth keeping the
        # target alive for, so it is cleared rather than cascading the delete
        # into a row that carries a summary.
        await session.execute(update(Article).where(Article.dup_of.in_(ids)).values(dup_of=None))
        # Two passes over the doomed set itself: a duplicate inside it points at
        # another member, and `PRAGMA foreign_keys=ON` refuses the delete in
        # whatever order SQLite picks.
        await session.execute(update(Article).where(Article.id.in_(ids)).values(dup_of=None))
        await session.execute(delete(Article).where(Article.id.in_(ids)))
    return len(ids)


async def _reclaim_checkpoints(settings: Settings, *, dry_run: bool) -> int:
    path = checkpoint_path(settings)
    if dry_run or not path.is_file():
        return 0
    before = path.stat().st_size
    with sqlite3.connect(path) as conn:
        conn.execute("VACUUM")
    return before - path.stat().st_size


async def _reclaim_database(*, dry_run: bool) -> int:
    """`VACUUM` on the app database, which cannot run inside a transaction."""
    if dry_run:
        return 0
    from ainews.db.session import get_engine

    engine = get_engine()
    async with engine.connect() as conn:
        autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
        before = (await autocommit.execute(text("PRAGMA page_count"))).scalar_one()
        size = (await autocommit.execute(text("PRAGMA page_size"))).scalar_one()
        await autocommit.execute(text("VACUUM"))
        after = (await autocommit.execute(text("PRAGMA page_count"))).scalar_one()
    return (before - after) * size
