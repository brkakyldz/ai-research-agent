"""The collect step: poll every enabled feed and store what is new.

This runs on its own every three hours with no LLM involved, and again as the
first node of the digest graph. Both paths call `collect_articles`, so there is
one implementation of "what does collection mean".

Failure is per-source, never per-run: a feed that 404s marks itself and the other
seventeen carry on. After `SOURCE_MAX_FAILURES` consecutive failures a source
disables itself, which is the only thing standing between a rotted feed and a log
line every three hours forever.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings, get_settings
from ainews.db import Article, Source
from ainews.db.models import utcnow
from ainews.sources.rss import FeedItem, FetchResult, fetch_feed, make_client

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CollectStats:
    n_sources: int = 0
    n_seen: int = 0
    n_new: int = 0
    n_not_modified: int = 0
    n_failed: int = 0
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def _too_old(item: FeedItem, settings: Settings) -> bool:
    """Skip archive entries. An item with no date is treated as current."""
    if item.published_at is None:
        return False
    horizon = utcnow() - timedelta(days=settings.collect_max_age_days)
    return item.published_at < horizon


async def _fetch_all(sources: list[Source]) -> list[tuple[Source, FetchResult]]:
    async with make_client() as client:

        async def one(source: Source) -> tuple[Source, FetchResult]:
            result = await fetch_feed(
                client, source.url, etag=source.etag, modified=source.modified
            )
            return source, result

        return list(await asyncio.gather(*(one(s) for s in sources)))


async def _record_failure(source: Source, result: FetchResult, settings: Settings) -> str:
    source.consecutive_failures += 1
    source.last_status = result.status
    source.last_fetched_at = utcnow()
    message = f"{source.name}: {result.error or result.status}"
    if source.consecutive_failures >= settings.source_max_failures:
        source.enabled = False
        message += f" (disabled after {source.consecutive_failures} consecutive failures)"
        log.warning("disabling source %s after repeated failures", source.name)
    return message


async def _insert_new_items(
    session: AsyncSession, source: Source, items: list[FeedItem], settings: Settings
) -> int:
    """Insert the items this source has not given us before.

    The canonical URL is unique across the whole table, so an article syndicated
    to three feeds is stored once and attributed to whichever feed reached us
    first. That is the URL-level half of deduplication; the fuzzy half runs later.
    """
    fresh = [i for i in items if not _too_old(i, settings)]
    if not fresh:
        return 0

    urls = [i.url_canonical for i in fresh]
    known: set[str] = set()
    # Chunked because the `IN` list is unbounded, not because 999 is the cap:
    # SQLite raised SQLITE_MAX_VARIABLE_NUMBER to 32766 in 3.32. One statement
    # per 500 feeds keeps the query planner's job small either way.
    for start in range(0, len(urls), 500):
        chunk = urls[start : start + 500]
        rows = await session.execute(
            select(Article.url_canonical).where(Article.url_canonical.in_(chunk))
        )
        known.update(rows.scalars())

    added = 0
    for item in fresh:
        if item.url_canonical in known:
            continue
        known.add(item.url_canonical)  # a feed can repeat a link within one payload
        session.add(
            Article(
                source_id=source.id,
                url=item.url,
                url_canonical=item.url_canonical,
                title=item.title,
                published_at=item.published_at,
                body_text=item.body_text,
            )
        )
        added += 1
    return added


async def collect_articles(session: AsyncSession, settings: Settings | None = None) -> CollectStats:
    """Poll every enabled source once and persist the new articles."""
    settings = settings or get_settings()
    sources = list(
        (await session.execute(select(Source).where(Source.enabled.is_(True)))).scalars()
    )
    stats = CollectStats(n_sources=len(sources))
    if not sources:
        log.warning("no enabled sources; nothing to collect")
        return stats

    for source, result in await _fetch_all(sources):
        if not result.ok:
            stats.n_failed += 1
            assert stats.errors is not None
            stats.errors.append(await _record_failure(source, result, settings))
            continue

        source.consecutive_failures = 0
        source.last_status = result.status
        source.last_fetched_at = utcnow()

        if result.not_modified:
            stats.n_not_modified += 1
            continue

        # Only overwrite the validators when the server actually sent them;
        # a feed that drops its ETag on one response should not lose ours.
        if result.etag:
            source.etag = result.etag
        if result.modified:
            source.modified = result.modified

        stats.n_seen += len(result.items)
        stats.n_new += await _insert_new_items(session, source, result.items, settings)

    await session.commit()
    log.info(
        "collect: %d sources, %d not modified, %d items seen, %d new, %d failed",
        stats.n_sources,
        stats.n_not_modified,
        stats.n_seen,
        stats.n_new,
        stats.n_failed,
    )
    return stats


async def probe_feed(url: str) -> FetchResult:
    """One-off fetch used by the /sources page when an operator adds a feed."""
    async with make_client() as client:
        try:
            return await fetch_feed(client, url)
        except httpx.HTTPError as exc:  # pragma: no cover - make_client rarely raises
            return FetchResult(ok=False, status=type(exc).__name__, error=str(exc)[:400])
