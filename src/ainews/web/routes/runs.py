"""The run log, and the button that starts one.

`POST /runs/start` is the "Run now" path. It calls exactly the function the
scheduler calls; the only difference is the `kind` written on the run row.

A run is started as a background task and the request returns at once, because a
digest takes about two minutes and an HTTP request that waits that long is a
request that times out somewhere in between. The button therefore reports "started",
and the page's own poll shows the result - which is also why a second press has to
be refused rather than queued.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings, get_settings
from ainews.db import Run, db_session
from ainews.pipeline.runner import digest_in_flight, release_digest, try_claim_digest
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

# The claim itself lives in `pipeline.runner`, because the 07:00 cron never goes
# through a route and has to take the same one. A second press while a run is in
# flight is refused, not queued.
# The event loop keeps only a weak reference to a task, so a fire-and-forget
# digest can be garbage-collected mid-run. Holding it here is what stops that.
_tasks: set[asyncio.Task[None]] = set()


def is_running() -> bool:

    return digest_in_flight()


async def _execute(language: str) -> None:
    from ainews.pipeline.runner import run_digest

    try:
        await run_digest(language=language, mode="manual")  # type: ignore[arg-type]
    except Exception:
        log.exception("manual run failed")


async def _guarded(language: str, token: str) -> None:
    """Hold the claim for exactly as long as the run lasts, however it ends."""

    try:
        await _execute(language)
    finally:
        release_digest(token)


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(
    request: Request,
    session: AsyncSession = Depends(db_session),
) -> HTMLResponse:
    language = language_of(request)
    runs = await queries.recent_runs(session, limit=40)
    context = base_context(request, language, page="runs")
    context.update(
        {
            "runs": runs,
            "failures": [r for r in runs if r.error][:8],
            "header": await build_header(session, language),
        }
    )
    response = get_templates().TemplateResponse(request, "runs.html", context)
    remember_language(response, language)
    return response


@router.post("/runs/start", response_class=HTMLResponse)
async def start_run(
    request: Request,
    lang: str | None = None,
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    language = lang if lang in ("tr", "en") else language_of(request)
    t = strings(language)  # type: ignore[arg-type]

    if not settings.llm_configured:
        return HTMLResponse(f'<span class="bad">{t["no_key"]}</span>')

    # Claim before creating the task, not inside it: `create_task` only schedules,
    # so a second press arriving before the task's first line would find the claim
    # still free and start a second - paid - digest.
    token = f"manual:{language}"
    if not await try_claim_digest(token):
        return HTMLResponse(t["busy"])
    # Fire and forget: the caller gets an answer now, the run finishes later.
    task = asyncio.create_task(_guarded(language, token))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)

    return HTMLResponse(
        f'<span hx-get="/runs/status?lang={language}" hx-trigger="every 3s" '
        f'hx-swap="outerHTML">{t["running"]}</span>'
    )


@router.get("/runs/status", response_class=HTMLResponse)
async def run_status(
    request: Request,
    lang: str | None = None,
    session: AsyncSession = Depends(db_session),
) -> HTMLResponse:
    """Polled by the strip until the run lands, then it reloads the page itself."""
    language = lang if lang in ("tr", "en") else language_of(request)
    t = strings(language)  # type: ignore[arg-type]

    if is_running():
        return HTMLResponse(
            f'<span hx-get="/runs/status?lang={language}" hx-trigger="every 3s" '
            f'hx-swap="outerHTML">{t["running"]}</span>'
        )

    latest = (
        await session.execute(
            select(Run).where(Run.kind == "manual").order_by(Run.started_at.desc()).limit(1)
        )
    ).scalar_one_or_none()

    label = t["status_error"] if latest is not None and latest.status == "error" else ""
    # The run is over, so the progress line stops and the page shows the result.
    return HTMLResponse(
        f'<span class="{"bad" if label else ""}">{label}</span>'
        "<script>document.getElementById('bar').classList.remove('is-running');"
        "setTimeout(() => location.reload(), 400);</script>"
    )
