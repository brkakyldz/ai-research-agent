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

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.db import Source

log = logging.getLogger(__name__)

FEEDS_FILE = Path(__file__).resolve().parent / "feeds.yaml"


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
