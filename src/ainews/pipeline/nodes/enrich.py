"""The enrich step: give thin articles a body before anything tries to summarise one.

Three tiers, cheapest first, and each one only runs when the one before it came
back short.

1. Clean the HTML the feed already sent. Free. Covers most feeds.
2. Fetch the article and run trafilatura over it. Costs a request. Covers Hacker
   News links and teaser-only feeds.
3. One capped Tavily news search. Costs a credit, so it is reserved for the items
   that are still empty *and* come from a source worth the credit.

The ordering is the whole design: enrichment is the only step here that can spend
money, and by the time it is reached most articles no longer need it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings, get_settings
from ainews.db import Article, Source
from ainews.sources import tavily
from ainews.sources.extract import clean_html, fetch_article, is_usable
from ainews.sources.rss import make_client

log = logging.getLogger(__name__)

# Only sources at or above this weight are worth a Tavily credit. Below it, a
# thin item is usually thin because it does not say much.
TAVILY_MIN_SOURCE_WEIGHT = 1.0
# Fetches run in threads; more than this and we are hammering a dozen hosts.
FETCH_CONCURRENCY = 6
# A title that is a name and a version number - `llm 0.35`, `llm-anthropic
# 0.28`, `llm-openrouter 0.7.1` - is not a news query. Searched as one on
# 2026-09-08 it returned everything on the web containing "0.28": a safety
# evaluation, a per-token price, a forum thread, and the summariser wrote a
# headline about an Anthropic evaluation that never happened. Such a title
# names a release, and the release note is either in the feed already or not
# on the news index at all; a credit spent here buys noise. Anchored at both
# ends so `$3.2 billion data center` and `Python 3.15 released` still qualify.
VERSION_TITLE = re.compile(r"^\S[^\n]{0,60}?\s+v?\d+\.\d+(?:\.\d+)*[\w.\-]*$")


@dataclass(slots=True)
class EnrichStats:
    n_examined: int = 0
    n_from_feed: int = 0
    n_fetched: int = 0
    n_tavily: int = 0
    n_still_empty: int = 0


async def enrich_articles(
    session: AsyncSession, article_ids: list[int], settings: Settings | None = None
) -> EnrichStats:
    """Fill in `body_text` for the given articles, as cheaply as each one allows."""
    settings = settings or get_settings()
    stats = EnrichStats()
    if not article_ids:
        return stats

    rows = list(
        (
            await session.execute(
                select(Article, Source.weight)
                .join(Source, Source.id == Article.source_id)
                .where(Article.id.in_(article_ids))
            )
        ).all()
    )
    stats.n_examined = len(rows)

    # Tier 1: the feed's own HTML.
    needs_fetch: list[tuple[Article, float]] = []
    for article, weight in rows:
        cleaned = clean_html(article.body_text)
        if is_usable(cleaned):
            article.body_text = cleaned
            stats.n_from_feed += 1
            continue
        # Keep the teaser: it is the fallback if the next two tiers fail.
        article.body_text = cleaned or None
        needs_fetch.append((article, weight))
    await session.commit()

    # Tier 2: fetch and extract, in parallel but not unboundedly. One client for
    # the whole pass, so ninety fetches share eight connections rather than
    # opening ninety - and it is the client the feed poll already uses.
    semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)

    still_thin: list[tuple[Article, float]] = []
    if needs_fetch:

        async def fetch_one(article: Article, client: httpx.AsyncClient) -> tuple[Article, str]:
            async with semaphore:
                return article, await fetch_article(article.url, client)

        async with make_client() as client:
            fetched = await asyncio.gather(*(fetch_one(a, client) for a, _ in needs_fetch))
        weights = {a.id: w for a, w in needs_fetch}
        for article, body in fetched:
            if is_usable(body):
                article.body_text = body
                stats.n_fetched += 1
            else:
                if body and len(body) > len(article.body_text or ""):
                    article.body_text = body
                still_thin.append((article, weights[article.id]))
        await session.commit()

    # Tier 3: Tavily, for the few that are still empty and worth a credit.
    for article, weight in still_thin:
        if weight < TAVILY_MIN_SOURCE_WEIGHT or VERSION_TITLE.match(article.title or ""):
            stats.n_still_empty += 1
            continue
        extra = await tavily.enrich(session, article.title, settings)
        if extra:
            article.body_text = f"{article.body_text or ''}\n\n{extra}".strip()
            stats.n_tavily += 1
        else:
            stats.n_still_empty += 1
    await session.commit()

    log.info(
        "enrich: %d examined, %d from feed, %d fetched, %d via tavily, %d still thin "
        "(%d tavily credits left today)",
        stats.n_examined,
        stats.n_from_feed,
        stats.n_fetched,
        stats.n_tavily,
        stats.n_still_empty,
        await tavily.remaining_credits(session, settings),
    )
    return stats
