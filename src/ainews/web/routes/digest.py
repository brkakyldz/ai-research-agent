"""The digest page, the archive and search.

Three routes because they are three URLs a person types or bookmarks, but one
template family: they all render the same story list, read at different times.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.db import Run, db_session
from ainews.web import queries
from ainews.web.views import (
    base_context,
    build_header,
    get_templates,
    language_of,
    remember_language,
)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    all: bool = Query(False, description="show everything summarised, not just the ranked top N"),
    tag: str | None = None,
    session: AsyncSession = Depends(db_session),
) -> HTMLResponse:
    language = language_of(request)
    run = await queries.latest_digest_run(session, language)

    context = base_context(request, language, page="digest")
    context.update(
        {
            "run": run,
            "editor_note": run.editor_note if run else None,
            "stories": [],
            "n_others": 0,
            "tags": [],
            "tag": tag,
            "show_all": all,
            "runs": [],
            "header": await build_header(session, language, run),
        }
    )

    if run is not None:
        stories = await queries.stories_for_run(session, run, ranked_only=not all, tag=tag)
        context["stories"] = stories
        # Only offer the expander when there is something behind it.
        context["n_others"] = 0 if all else await queries.count_unranked(session, run)
        context["tags"] = await queries.tag_counts(session, run)
        context["runs"] = await queries.recent_runs(session, limit=8)

    response = get_templates().TemplateResponse(request, "index.html", context)
    remember_language(response, language)
    return response


@router.get("/archive", response_class=HTMLResponse)
async def archive(
    request: Request,
    run: str | None = None,
    session: AsyncSession = Depends(db_session),
) -> HTMLResponse:
    language = language_of(request)
    runs = await queries.digest_runs(session, language)
    selected: Run | None = None
    if runs:
        selected = next((r for r in runs if r.id == run), runs[0])

    context = base_context(request, language, page="archive")
    context.update(
        {
            "runs": runs,
            "selected": selected,
            "stories": await queries.stories_for_run(session, selected) if selected else [],
            "header": await build_header(session, language, selected),
        }
    )
    response = get_templates().TemplateResponse(request, "archive.html", context)
    remember_language(response, language)
    return response


@router.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    q: str = "",
    session: AsyncSession = Depends(db_session),
) -> HTMLResponse:
    language = language_of(request)
    context = base_context(request, language, page="search")
    context.update(
        {
            "query": q,
            "stories": await queries.search_stories(session, q, language) if q.strip() else [],
            "header": await build_header(session, language),
        }
    )
    response = get_templates().TemplateResponse(request, "search.html", context)
    remember_language(response, language)
    return response
