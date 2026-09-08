"""The sources page: what is being polled, what it last said, and three controls.

Enabling and disabling is a form post rather than an HTMX swap, because the row
it changes is one of sixteen in a table that has to be re-sorted anyway - a full
render is both simpler and correct.

Weight is the third, and it is the one this page was missing. It breaks ranking
ties, decides which thin article is worth a Tavily credit, and since ADR 0025
picks the survivor of a duplicate cluster - three editorial jobs, on a number
that was documented as a range, constrained only above zero, and settable from
nowhere in the running app. `add_source` wrote 1.0 and that was the end of it.

There is still no delete, and that is a decision rather than a gap. A disabled
feed stays in the table at a lower ink step because it is a choice the reader
made and can see; a deleted one would take its articles' foreign key with it, or
leave the archive pointing at a source that no longer has a name.

Adding a feed probes it first. A URL that does not parse as a feed is rejected at
the moment it is typed, which is the only moment anyone is in a position to fix it.
A blocked host is rejected before the probe, because no answer it gives would
change the decision.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.db import Source, db_session
from ainews.pipeline.api import probe_feed
from ainews.sources.seed import is_blocked
from ainews.web import queries
from ainews.web.i18n import strings
from ainews.web.views import (
    get_templates,
    language_of,
    remember_preferences,
    shell_context,
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
    context = await shell_context(request, session, language, page="sources")
    context.update(
        {
            "sources": await queries.source_rows(session),
            "weight_range": (WEIGHT_MIN, WEIGHT_MAX, WEIGHT_STEP),
            "message": message,
            "message_bad": bad,
        }
    )
    response = get_templates(request).TemplateResponse(request, "sources.html", context)
    remember_preferences(request, response, language)
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


# The range the column actually holds, and the range the model's CHECK enforces.
#
# This was a four-step `<select>` for about ten minutes, on the reasoning that a
# person setting an editorial weight is answering a coarse question. The seed
# file answers it in tenths - 0.9, 1.2, 1.3, 1.4, 1.6, 1.8 - so nine of the
# sixteen rows had no matching `<option>`, the browser selected the first one for
# them, and the first save silently wrote 0.5 over every weight the seed had
# tuned. A control whose vocabulary is narrower than its column's does not
# restrict input, it destroys it on submit.
WEIGHT_MIN = 0.5
WEIGHT_MAX = 2.0
WEIGHT_STEP = 0.1


@router.post("/sources/weights")
async def set_weights(
    request: Request,
    session: AsyncSession = Depends(db_session),
) -> RedirectResponse:
    """Every weight on the page, saved at once.

    One form for the whole column and not one per row, because weights are
    editorial and relative: the question is which of these sixteen feeds leads,
    and answering it one round trip at a time makes the reader hold the other
    fifteen in their head. The selects live inside the table and the form does
    not - HTML's `form` attribute is what lets a control belong to a form it is
    not nested in, and it is also what keeps these out of the toggle form each
    row already carries, since a form cannot contain another.

    An id that is not a source, a value outside 0.5-2.0, and a value equal to
    what is already stored are all skipped rather than refused: this is a bulk
    submit and every row is in it whether or not it was touched. A field left
    empty or typed into badly therefore leaves its row alone, which is the only
    safe reading of it - the alternative is a form that quietly rewrites fifteen
    weights because one of them would not parse.
    """
    form = await request.form()
    language = _valid(str(form.get("lang", "tr")))
    changed = 0
    for key, raw in form.items():
        if not key.startswith("w_"):
            continue
        try:
            source_id, weight = int(key[2:]), round(float(str(raw)), 1)
        except ValueError:
            continue
        if not WEIGHT_MIN <= weight <= WEIGHT_MAX:
            continue
        source = await session.get(Source, source_id)
        if source is not None and source.weight != weight:
            source.weight = weight
            changed += 1
    await session.commit()

    t = strings(language)  # type: ignore[arg-type]
    message = t["weights_saved"].format(n=changed) if changed else t["weights_unchanged"]
    return RedirectResponse(f"/sources?lang={language}&message={message}", status_code=303)


def _valid(language: str) -> str:
    return language if language in ("tr", "en") else "tr"


@router.post("/sources/add")
async def add_source(
    url: str = Form(...),
    lang: str = Form("tr"),
    session: AsyncSession = Depends(db_session),
) -> RedirectResponse:
    t = strings(_valid(lang))  # type: ignore[arg-type]
    url = url.strip()

    if is_blocked(url):
        return RedirectResponse(
            f"/sources?lang={lang}&bad=true&message={t['feed_blocked']}", status_code=303
        )

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
