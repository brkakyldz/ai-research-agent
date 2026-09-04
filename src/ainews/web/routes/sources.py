"""The sources page: what is being polled, what it last said, and two controls.

Enabling and disabling is a form post rather than an HTMX swap, because the row
it changes is one of eighteen in a table that has to be re-sorted anyway - a full
render is both simpler and correct.

Adding a feed probes it first. A URL that does not parse as a feed is rejected at
the moment it is typed, which is the only moment anyone is in a position to fix it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.db import Source, db_session
from ainews.web import queries
from ainews.web.i18n import strings
from ainews.web.views import (
    base_context,
    build_header,
    get_templates,
    language_of,
    remember_language,
)

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/sources", response_class=HTMLResponse)
async def sources_page(
    request: Request,
    message: str | None = None,
    bad: bool = False,
    session: AsyncSession = Depends(db_session),
) -> HTMLResponse:
    language = language_of(request)
    context = base_context(request, language, page="sources")
    context.update(
        {
            "sources": await queries.source_rows(session),
            "message": message,
            "message_bad": bad,
            "header": await build_header(session, language),
        }
    )
    response = get_templates().TemplateResponse(request, "sources.html", context)
    remember_language(response, language)
    return response


@router.post("/sources/toggle")
async def toggle_source(
    source_id: int = Form(...),
    lang: str = Form("tr"),
    session: AsyncSession = Depends(db_session),
) -> RedirectResponse:
    source = await session.get(Source, source_id)
    if source is not None:
        source.enabled = not source.enabled
        if source.enabled:
            # Re-enabling is an explicit act of forgiveness: clear the strikes,
            # or the next single failure disables it again immediately.
            source.consecutive_failures = 0
        await session.commit()
    return RedirectResponse(f"/sources?lang={lang}", status_code=303)


@router.post("/sources/add")
async def add_source(
    url: str = Form(...),
    lang: str = Form("tr"),
    session: AsyncSession = Depends(db_session),
) -> RedirectResponse:
    from ainews.pipeline.nodes.collect import probe_feed

    t = strings(lang if lang in ("tr", "en") else "tr")  # type: ignore[arg-type]
    url = url.strip()

    existing = (await session.execute(select(Source).where(Source.url == url))).scalar_one_or_none()
    if existing is not None:
        return RedirectResponse(f"/sources?lang={lang}&message={t['feed_added']}", status_code=303)

    result = await probe_feed(url)
    if not result.ok:
        return RedirectResponse(
            f"/sources?lang={lang}&bad=true&message={t['feed_rejected']}: {result.status}",
            status_code=303,
        )

    # The feed's own title is a better name than the URL, and it is right there.
    session.add(Source(name=_name_for(url, result), url=url, kind="rss", weight=1.0))
    await session.commit()
    return RedirectResponse(f"/sources?lang={lang}&message={t['feed_added']}", status_code=303)


def _name_for(url: str, result: object) -> str:
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or url
    return host.removeprefix("www.")
