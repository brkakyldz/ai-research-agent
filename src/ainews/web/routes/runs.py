"""The run log, the button that starts one, and the question in front of it.

`POST /runs/start` is the only thing that starts a digest in the running app
(ADR 0015). It calls exactly the function the CLI calls; the only difference is
the `kind` written on the run row. When it is worth pressing is the advice block
above it, computed in `views.build_advice` and never enforced here: this route
refuses a press for two reasons only, a run in flight and a missing key.

The press carries one choice: which language the bulletin will be written in.
It lives here and not in the bar's TR/EN switch, which translates the interface
and nothing else (ADR 0017) - a preference about what the buttons say has no
business deciding what a paid run produces. The choice travels as `?out=`,
defaults to the language the page is drawn in, and is not remembered: it is a
property of this press, so the next one asks again.

Nothing reaches it in one click. `GET /runs/confirm` and `GET /runs/action` are
the two halves of a confirmation that lives in the page rather than in a dialog -
the button swaps itself for a question, and the question swaps back. Two routes
and no JavaScript, because the strings in the question are translated and the
one place they are allowed to live is `i18n`.

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
    build_advice,
    count_enabled_sources,
    get_templates,
    language_of,
    remember_preferences,
    shell_context,
)

log = logging.getLogger(__name__)
router = APIRouter()

# The claim itself lives in `pipeline.runner`, because the CLI never goes through
# a route and has to take the same one. A second press while a run is in flight is
# refused, not queued.
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


def _valid(value: str | None, fallback: str) -> str:
    return value if value in ("tr", "en") else fallback


async def _action_context(
    request: Request,
    session: AsyncSession,
    language: str,
    asking: bool,
    out: str,
) -> dict[str, object]:
    """Everything `_run_action.html` needs, and it is the same set every time.

    The cost shown in the question is the last run's, not an average and not a
    guess: a number a reader can check against the table underneath is worth
    more than a tighter estimate they cannot.
    """
    advice = await build_advice(session, language)  # type: ignore[arg-type]
    return {
        "request": request,
        "language": language,
        "t": strings(language),  # type: ignore[arg-type]
        "advice": advice,
        "asking": asking,
        "out": out,
        "n_sources": await count_enabled_sources(session),
        "last_cost": advice.last.est_cost_usd if advice.last else None,
    }


async def _render_action(
    request: Request,
    session: AsyncSession,
    language: str,
    asking: bool,
    out: str,
) -> HTMLResponse:
    context = await _action_context(request, session, language, asking, out)
    return get_templates().TemplateResponse(request, "_run_action.html", context)


@router.get("/runs/action", response_class=HTMLResponse)
async def run_action(
    request: Request,
    lang: str | None = None,
    out: str | None = None,
    session: AsyncSession = Depends(db_session),
) -> HTMLResponse:
    """The resting state: the button that asks. Also where `vazgeç` lands."""
    language = _valid(lang, language_of(request))
    return await _render_action(request, session, language, asking=False, out=_valid(out, language))


@router.get("/runs/confirm", response_class=HTMLResponse)
async def run_confirm(
    request: Request,
    lang: str | None = None,
    out: str | None = None,
    session: AsyncSession = Depends(db_session),
) -> HTMLResponse:
    """The question. Pressing the button gets here and no further.

    The two output-language slots re-enter this same route with a different
    `?out=`, which is why choosing one costs no JavaScript and no client state:
    the fragment is re-rendered with the other slot marked, exactly the way the
    question itself is swapped in and out.
    """
    language = _valid(lang, language_of(request))
    return await _render_action(request, session, language, asking=True, out=_valid(out, language))


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(
    request: Request,
    session: AsyncSession = Depends(db_session),
) -> HTMLResponse:
    language = language_of(request)
    runs = await queries.recent_runs(session, limit=40)
    context = await shell_context(request, session, language, page="runs")
    context.update(
        {
            "runs": runs,
            "failures": [r for r in runs if r.error][:8],
            # The spend totals used to head the digest's side column. They are
            # execution metrics, which ADR 0009 put on this page and this page
            # alone; the side column had kept a copy.
            "activity": await queries.recent_activity(session),
        }
    )
    response = get_templates().TemplateResponse(request, "runs.html", context)
    remember_preferences(request, response, language)
    return response


@router.post("/runs/start", response_class=HTMLResponse)
async def start_run(
    request: Request,
    lang: str | None = None,
    out: str | None = None,
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    language = _valid(lang, language_of(request))
    # What the run writes, which is not what the page says. A press with no
    # `?out=` - the CLI's shape, or a stale fragment - falls back to the page's
    # language, so the old single-value behaviour is still the default rather
    # than an error.
    produce = _valid(out, language)
    t = strings(language)  # type: ignore[arg-type]

    if not settings.llm_configured:
        return HTMLResponse(f'<span class="bad">{t["no_key"]}</span>')

    # Claim before creating the task, not inside it: `create_task` only schedules,
    # so a second press arriving before the task's first line would find the claim
    # still free and start a second - paid - digest.
    token = f"manual:{produce}"
    if not await try_claim_digest(token):
        return HTMLResponse(t["busy"])
    # Fire and forget: the caller gets an answer now, the run finishes later.
    task = asyncio.create_task(_guarded(produce, token))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)

    # Only the bar's status line. Putting the question back to rest is the
    # button's own follow-up request, not this route's job: this one is on the
    # path a second press races against, and it has no business opening a
    # session to render a fragment while it holds the claim.
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
    language = _valid(lang, language_of(request))
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
