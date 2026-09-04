"""Liveness and configuration probe.

`/health` answers the two questions worth asking of a local tool that has just
started: is the database reachable, and are the API keys actually present. It
never returns the keys themselves, only whether they are set.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
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


@router.get("/", response_class=HTMLResponse)
async def index() -> str:
    """Placeholder until M4 replaces it with the digest page."""
    return (
        "<!doctype html><meta charset='utf-8'><title>AI News Digest</title>"
        "<body style='font:14px/1.6 system-ui;max-width:40rem;margin:4rem auto;color:#ddd;"
        "background:#111'>"
        "<h1 style='font-size:1.1rem'>AI News Digest</h1>"
        "<p>Scaffold is up. The digest page lands in M4 &mdash; "
        "<a href='/health' style='color:#ddd'>/health</a> works now.</p>"
    )
