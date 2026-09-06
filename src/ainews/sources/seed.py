"""Seeding the `sources` table from `feeds.yaml`.

The file is the shipped default; the table is the operator's copy. Sync is
therefore one-way and additive: a feed in the file that is missing from the table
is inserted, and everything else is left exactly as the operator left it. A feed
disabled in `/sources` stays disabled across restarts, which it would not if this
were an upsert.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.db import Source

log = logging.getLogger(__name__)

FEEDS_FILE = Path(__file__).resolve().parent / "feeds.yaml"

# Hosts that may not be added at all, not even by hand in /sources. Both were
# shipped defaults until 2026-09-04 and both were removed for the same reason:
# a firehose of comment threads whose items are a title plus someone else's
# link, so the ones that rank cost an extra extraction round and still summarise
# into a restatement of the article they point at. This is deliberately a host
# rule rather than a URL rule - hnrss.org alone serves a dozen query variants of
# the same feed, and Reddit one per subreddit.
BLOCKED_HOSTS = frozenset({"hnrss.org", "news.ycombinator.com", "reddit.com"})


def is_blocked(url: str) -> bool:
    """True if `url` names a host the digest refuses to poll."""
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.").removeprefix("old.")
    return host in BLOCKED_HOSTS


def load_seed_feeds(path: Path | None = None) -> list[dict[str, object]]:
    raw = yaml.safe_load((path or FEEDS_FILE).read_text(encoding="utf-8")) or []
    feeds: list[dict[str, object]] = []
    for entry in raw:
        if not entry.get("name") or not entry.get("url"):
            raise ValueError(f"feed entry missing name or url: {entry!r}")
        feeds.append(
            {
                "name": str(entry["name"]),
                "url": str(entry["url"]),
                "kind": str(entry.get("kind", "rss")),
                "weight": float(entry.get("weight", 1.0)),
            }
        )
    return feeds


async def sync_sources(session: AsyncSession, path: Path | None = None) -> int:
    """Insert any seed feed the table does not have yet. Returns how many were added."""
    feeds = load_seed_feeds(path)
    existing = set((await session.execute(select(Source.url))).scalars())
    added = 0
    for feed in feeds:
        if feed["url"] in existing:
            continue
        session.add(Source(**feed))
        added += 1
    if added:
        await session.commit()
        log.info("seeded %d new source(s) from %s", added, (path or FEEDS_FILE).name)
    return added
