"""Tavily enrichment, behind a credit cap that survives a restart.

Tavily is the last resort, not a source. It is reached only when a feed gave us
a headline and no body and trafilatura could not fetch one either - typically a
linkblog pointing at a page that blocks scrapers.

The free tier is 1,000 credits a month, so the cap is the whole point of this
module. It lives in the `daily_counters` table rather than in a module-level
integer, because a process that restarts twice a day would otherwise reset its
own budget twice a day and quietly spend the month's credits in a week.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings, get_settings
from ainews.db import DailyCounter

log = logging.getLogger(__name__)

# Basic search is 1 credit; advanced is 2 and buys nothing we need here.
SEARCH_DEPTH = "basic"
MAX_RESULTS = 3


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


async def credits_used_today(session: AsyncSession) -> int:
    row = await session.get(DailyCounter, _today())
    return row.tavily_credits if row else 0


async def _reserve_credit(session: AsyncSession, settings: Settings) -> bool:
    """Take one credit if the cap allows. Reserved before the call, not after.

    Charging first means a request that times out still costs its credit, which
    matches what Tavily bills and keeps a failing endpoint from being retried
    into the monthly allowance.
    """
    day = _today()
    row = await session.get(DailyCounter, day)
    if row is None:
        row = DailyCounter(day=day, tavily_credits=0)
        session.add(row)
        await session.flush()
    if row.tavily_credits >= settings.tavily_daily_cap:
        return False
    row.tavily_credits += 1
    await session.commit()
    return True


def _search_sync(query: str, settings: Settings) -> str:
    from langchain_tavily import TavilySearch

    # No `include_answer`, since 2026-09-08. The answer is Tavily's own model
    # writing a paragraph over whatever the search returned, and for a query
    # that matched nothing relevant it writes a confident paragraph about the
    # noise: the run of that day appended "the LLM model identified as '0.28'
    # refers to Claude Sonnet 5" to a plugin release note titled
    # `llm-anthropic 0.28`, the summariser summarised it, and the grounding
    # judge - which reads the stored body - would have called it grounded.
    # Snippets are at least text someone published; the answer was not.
    tool = TavilySearch(
        tavily_api_key=settings.tavily_api_key,
        max_results=MAX_RESULTS,
        search_depth=SEARCH_DEPTH,
        topic="news",
        time_range="week",
    )
    payload = tool.invoke({"query": query})
    if not isinstance(payload, dict):
        return ""

    parts: list[str] = []
    for result in payload.get("results") or []:
        snippet = (result.get("content") or "").strip()
        if snippet:
            parts.append(snippet)
    return "\n\n".join(parts)[:4000]


async def enrich(session: AsyncSession, query: str, settings: Settings | None = None) -> str:
    """One capped news search. Returns "" when disabled, capped, or failing."""
    settings = settings or get_settings()
    if not settings.tavily_configured:
        return ""
    if not await _reserve_credit(session, settings):
        log.info("tavily daily cap of %d credits reached; skipping", settings.tavily_daily_cap)
        return ""

    try:
        return await asyncio.to_thread(_search_sync, query, settings)
    except Exception as exc:
        # A failed enrichment degrades the summary; it must not fail the run.
        log.warning("tavily search failed for %r: %s", query[:80], exc)
        return ""


async def remaining_credits(session: AsyncSession, settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    used = await credits_used_today(session)
    return max(0, settings.tavily_daily_cap - used)


async def counters_for_day(session: AsyncSession, day: str | None = None) -> DailyCounter | None:
    return (
        await session.execute(select(DailyCounter).where(DailyCounter.day == (day or _today())))
    ).scalar_one_or_none()
