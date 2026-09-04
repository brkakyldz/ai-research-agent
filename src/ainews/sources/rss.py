"""Feed fetching.

Two things here are not optional. The first is the conditional GET: feedparser's
own documentation warns that publishers ban clients that re-download an unchanged
feed, and this one polls eighteen of them every three hours, so the stored `etag`
and `Last-Modified` go back out on every request and a 304 costs nothing.

The second is the User-Agent. The Verge and Ars Technica reject the default
Python one outright; a browser string is what makes those two feeds exist for us.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import feedparser
import httpx

from ainews.sources.urls import canonical_url

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
REQUEST_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


@dataclass(slots=True)
class FeedItem:
    """One entry, reduced to what the pipeline actually stores."""

    title: str
    url: str
    url_canonical: str
    published_at: datetime | None
    body_text: str | None


@dataclass(slots=True)
class FetchResult:
    """Outcome of one feed fetch - success, 304, or a reason it failed."""

    ok: bool
    status: str
    items: list[FeedItem] = field(default_factory=list)
    etag: str | None = None
    modified: str | None = None
    not_modified: bool = False
    error: str | None = None


def _parse_published(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=UTC)
            except (TypeError, ValueError):
                continue
    return None


def _entry_text(entry: Any) -> str | None:
    """Best available body text from the feed itself, before any web fetch.

    Many feeds ship the full post in `content`; the rest ship a teaser in
    `summary`. Either way this is HTML, and it is cleaned in `extract.py` - the
    job here is only to find the richest field.
    """
    contents = entry.get("content") or []
    if contents:
        best = max(contents, key=lambda c: len(c.get("value") or ""))
        value = best.get("value")
        if value:
            return value
    for key in ("summary", "description", "subtitle"):
        value = entry.get(key)
        if value:
            return value
    return None


def parse_feed_bytes(payload: bytes) -> list[FeedItem]:
    """Turn raw feed bytes into items. Pure and synchronous, so it is easy to test."""
    parsed = feedparser.parse(payload)
    items: list[FeedItem] = []
    for entry in parsed.entries:
        link = (entry.get("link") or "").strip()
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue
        items.append(
            FeedItem(
                title=title,
                url=link,
                url_canonical=canonical_url(link),
                published_at=_parse_published(entry),
                body_text=_entry_text(entry),
            )
        )
    return items


async def fetch_feed(
    client: httpx.AsyncClient,
    url: str,
    *,
    etag: str | None = None,
    modified: str | None = None,
) -> FetchResult:
    """Fetch one feed, honouring and returning its cache validators."""
    headers = dict(DEFAULT_HEADERS)
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified

    try:
        response = await client.get(url, headers=headers, follow_redirects=True)
    except httpx.HTTPError as exc:
        return FetchResult(ok=False, status=type(exc).__name__, error=str(exc)[:400])

    if response.status_code == 304:
        return FetchResult(
            ok=True,
            status="304 Not Modified",
            not_modified=True,
            etag=etag,
            modified=modified,
        )

    if response.status_code >= 400:
        return FetchResult(
            ok=False,
            status=f"HTTP {response.status_code}",
            error=f"HTTP {response.status_code} for {url}",
        )

    # feedparser is CPU-bound C and pure Python; keep it off the event loop.
    items = await asyncio.to_thread(parse_feed_bytes, response.content)
    if not items:
        return FetchResult(
            ok=False,
            status=f"HTTP {response.status_code}, 0 items",
            error="feed parsed but contained no usable entries",
        )

    return FetchResult(
        ok=True,
        status=f"HTTP {response.status_code}, {len(items)} items",
        items=items,
        etag=response.headers.get("ETag"),
        modified=response.headers.get("Last-Modified"),
    )


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        headers=DEFAULT_HEADERS,
        follow_redirects=True,
        # Eighteen feeds against a dozen hosts; a small pool is plenty and keeps
        # us from looking like a scraper.
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
    )
