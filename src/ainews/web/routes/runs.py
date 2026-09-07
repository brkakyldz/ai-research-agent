"""The run log, the button that starts one, and the question in front of it.

`POST /runs/start` is the only thing that starts a digest in the running app
(ADR 0015). It calls exactly the function the CLI calls; the only difference is
the `kind` written on the run row. When it is worth pressing is the advice block
above it, computed in `views.build_advice` and never enforced here: this route
refuses a press for two reasons only, a run in flight and a missing key.

The press carries three choices, and they are all the same kind of thing: a
property of *this* press, asked next to the money, defaulting to the environment
and remembered nowhere.

`?out=` is which language the bulletin will be written in. It lives here and not
in the bar's TR/EN switch, which translates the interface and nothing else
(ADR 0017) - a preference about what the buttons say has no business deciding
what a paid run produces.

`?ms=` and `?mr=` are which model summarises and which model ranks (ADR 0020).
Two and not one because the pipeline has always had two knobs for a reason
(ADR 0001): the summariser is the ninety-odd calls and the language risk, the
ranker is one call over the whole day, and the case for moving them is not the
same case. An unknown name falls back to the configured default rather than
erroring - `resolve_model` says why.

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
from ainews.pipeline.llm import model_options, resolve_model
from ainews.pipeline.runner import digest_in_flight, release_digest, try_claim_digest
from ainews.web import queries
from ainews.web.i18n import LANGUAGES, strings
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


async def _execute(language: str, models: tuple[str, str]) -> None:
    from ainews.pipeline.runner import run_digest

    try:
        await run_digest(
            language=language,  # type: ignore[arg-type]
            mode="manual",
            model_summarize=models[0],
            model_rank=models[1],
        )
    except Exception:
        log.exception("manual run failed")


async def _guarded(language: str, models: tuple[str, str], token: str) -> None:
    """Hold the claim for exactly as long as the run lasts, however it ends."""

    try:
        await _execute(language, models)
    finally:
        release_digest(token)


def _valid(value: str | None, fallback: str) -> str:
    return value if value in LANGUAGES else fallback


def _models(ms: str | None, mr: str | None, settings: Settings) -> tuple[str, str]:
    """The two chosen models, resolved against the environment's defaults."""
    return (
        resolve_model(ms, settings.openai_model_summarize),
        resolve_model(mr, settings.openai_model),
    )


async def _action_context(
    request: Request,
    session: AsyncSession,
    language: str,
    asking: bool,
    out: str,
    models: tuple[str, str],
    settings: Settings,
) -> dict[str, object]:
    """Everything `_run_action.html` needs, and it is the same set every time.

    The cost shown in the question is the last run's, not an average and not a
    guess: a number a reader can check against the table underneath is worth
    more than a tighter estimate they cannot. The prices beside the model slots
    are the other half of that: the last run's cost only means something next to
    what the next one is priced at, and this is the screen where a reader can
    pick a tier that costs fifty times as much without being told.

    No projection is drawn from the two together, though the arithmetic is
    tempting. The run row keeps one token total, not a per-node split, so
    multiplying it by a new pair of prices would be a number with a decimal
    point and no basis. Two honest facts beat one invented one.
    """
    advice = await build_advice(session, language)  # type: ignore[arg-type]
    options = model_options(settings.openai_model_summarize, settings.openai_model)
    by_id = {choice.id: choice for choice in options}
    return {
        "request": request,
        "language": language,
        "t": strings(language),  # type: ignore[arg-type]
        "advice": advice,
        "asking": asking,
        "out": out,
        "n_sources": await count_enabled_sources(session),
        "last_cost": advice.last.est_cost_usd if advice.last else None,
        "models": options,
        "ms": models[0],
        "mr": models[1],
        "ms_price": by_id[models[0]],
        "mr_price": by_id[models[1]],
    }


async def _render_action(
    request: Request,
    session: AsyncSession,
    language: str,
    asking: bool,
    out: str,
    models: tuple[str, str],
    settings: Settings,
) -> HTMLResponse:
    context = await _action_context(request, session, language, asking, out, models, settings)
    return get_templates().TemplateResponse(request, "_run_action.html", context)


@router.get("/runs/action", response_class=HTMLResponse)
async def run_action(
    request: Request,
    lang: str | None = None,
    out: str | None = None,
    ms: str | None = None,
    mr: str | None = None,
    session: AsyncSession = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """The resting state: the button that asks. Also where `vazgeç` lands."""
    language = _valid(lang, language_of(request))
    return await _render_action(
        request,
        session,
        language,
        asking=False,
        out=_valid(out, language),
        models=_models(ms, mr, settings),
        settings=settings,
    )


@router.get("/runs/confirm", response_class=HTMLResponse)
async def run_confirm(
    request: Request,
    lang: str | None = None,
    out: str | None = None,
    ms: str | None = None,
    mr: str | None = None,
    session: AsyncSession = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """The question. Pressing the button gets here and no further.

    Every slot in the question - the two output languages, the two rows of
    models - re-enters this same route with one parameter changed and the others
    carried through, which is why choosing costs no JavaScript and no client
    state: the fragment is re-rendered with a different slot marked, exactly the
    way the question itself is swapped in and out. Carrying the others through
    is the whole trick; a slot that forgot the neighbouring choice would quietly
    reset a model to the default on the way to picking a language.
    """
    language = _valid(lang, language_of(request))
    return await _render_action(
        request,
        session,
        language,
        asking=True,
        out=_valid(out, language),
        models=_models(ms, mr, settings),
        settings=settings,
    )


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
    ms: str | None = None,
    mr: str | None = None,
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    language = _valid(lang, language_of(request))
    # What the run writes, which is not what the page says. A press with no
    # `?out=` - the CLI's shape, or a stale fragment - falls back to the page's
    # language, so the old single-value behaviour is still the default rather
    # than an error. The models resolve the same way and for the same reason: a
    # press that names nothing is the press this route answered before ADR 0020.
    produce = _valid(out, language)
    models = _models(ms, mr, settings)
    t = strings(language)  # type: ignore[arg-type]

    if not settings.llm_configured:
        return HTMLResponse(f'<span class="bad">{t["no_key"]}</span>')

    # Claim before creating the task, not inside it: `create_task` only schedules,
    # so a second press arriving before the task's first line would find the claim
    # still free and start a second - paid - digest.
    token = f"manual:{produce}:{models[0]}:{models[1]}"
    if not await try_claim_digest(token):
        return HTMLResponse(t["busy"])
    # Fire and forget: the caller gets an answer now, the run finishes later.
    task = asyncio.create_task(_guarded(produce, models, token))
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


# Registered last on purpose. FastAPI matches routes in the order they are
# declared, and `{run_id}` would otherwise swallow `/runs/action`, `/runs/confirm`
# and `/runs/status` - a path parameter with no pattern matches any segment, and
# the literal routes above are the ones that have to win.
@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(
    request: Request,
    run_id: str,
    session: AsyncSession = Depends(db_session),
) -> HTMLResponse:
    """One run, node by node: where its time and its money went (ADR 0022).

    A run started before `run_steps` existed - or one that died before its first
    node closed - renders with an empty step list and says so, rather than 404ing
    on a run row that is plainly there in the table above.
    """
    language = language_of(request)
    t = strings(language)  # type: ignore[arg-type]
    run = await queries.run_by_id(session, run_id)
    context = await shell_context(request, session, language, page="runs")
    if run is None:
        response = get_templates().TemplateResponse(
            request, "run_missing.html", context, status_code=404
        )
        remember_preferences(request, response, language)
        return response

    steps = await queries.steps_for_run(session, run_id)
    # The share each node took of the run's wall clock, computed here rather than
    # in the template because a division with a zero guard is not markup. The
    # denominator is the run's own duration and not the sum of the steps: the two
    # differ by the scheduling between supersteps, and a bar that normalised the
    # gap away would claim the pipeline is busy when it is waiting.
    span = run.duration_seconds or 0.0
    context.update(
        {
            "run": run,
            "steps": [
                {"step": s, "share": (s.duration_seconds or 0.0) / span if span else 0.0}
                for s in steps
            ],
            "tokens": run.tokens_in + run.tokens_out,
            # Which models actually ran, which until now nothing on screen could
            # say: ADR 0020 made the model a choice at the press, and `runs` has
            # no column for it - `run_steps` is the first place it is written
            # down. Short names, because the tier is the whole question and the
            # ids differ in exactly that segment.
            "models": [(t["step_" + s.node], s.model.rsplit("-", 1)[-1]) for s in steps if s.model],
        }
    )
    response = get_templates().TemplateResponse(request, "run_detail.html", context)
    remember_preferences(request, response, language)
    return response
