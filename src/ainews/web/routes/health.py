"""Liveness and configuration probe.

`/health` answers the two questions worth asking of a local tool that has just
started: is the database reachable, and are the API keys actually present. It
never returns the keys themselves, only whether they are set.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings, get_settings
from ainews.db import Article, Run, Source, db_session

router = APIRouter()


@router.get("/health")
async def health(
    session: AsyncSession = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    counts = {}
    for label, model in (("sources", Source), ("articles", Article), ("runs", Run)):
        result = await session.execute(select(func.count()).select_from(model))
        counts[label] = result.scalar_one()
    return {
        "status": "ok",
        "version": "0.1.0",
        "database": str(settings.sqlite_path),
        "counts": counts,
        "llm_configured": settings.llm_configured,
        "tavily_configured": settings.tavily_configured,
    }
