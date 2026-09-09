"""What a local tool is allowed to forget.

The archive is the point of this thing and nothing here touches it: a summary,
a run row, a bulletin, a verdict and a judge's finding are all kept forever, and
the front page, `/archive`, the search index and every evaluation number are read
off them. One thing accumulates that no reader ever reaches.

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
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select, text, update

from ainews.config import Settings, get_settings
from ainews.db import Article, Summary
from ainews.db.models import utcnow
from ainews.db.session import session_scope
from ainews.pipeline.runner import RunBusy, run_in_flight

log = logging.getLogger(__name__)


@dataclass(slots=True)
class PruneReport:
    """What was removed, or would be. Every number is counted before the delete,
    so `--dry-run` and the real thing report the same figures."""

    articles: int
    database_bytes: int
    dry_run: bool

    @property
    def total_bytes(self) -> int:
        return self.database_bytes


async def prune(settings: Settings | None = None, *, dry_run: bool = False) -> PruneReport:
    """Drop the dead articles nothing can reach.

    Refuses while a run holds the slot: a digest in flight is collecting the
    very articles this would count, and an article summarised a second after
    the count is one this would then delete out from under a live summary.
    """
    settings = settings or get_settings()
    if run_in_flight():
        raise RunBusy("a run is in flight; prune when it has finished")

    articles = await _prune_articles(settings, dry_run=dry_run)
    return PruneReport(
        articles=articles,
        database_bytes=await _reclaim_database(dry_run=dry_run),
        dry_run=dry_run,
    )


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
