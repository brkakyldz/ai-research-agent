"""The run log, the button that starts one, and the question in front of it.

`POST /runs/start` is the only thing that starts a digest in the running app
(ADR 0015). It calls exactly the function the CLI calls, and since ADR 0026 the
run row it writes is identical too. When it is worth pressing is the advice block
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
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings, get_settings
from ainews.db import bulletin_runs, db_session
from ainews.pipeline.llm import model_options, resolve_model
from ainews.pipeline.runner import (
    digest_in_flight,
    release_slot,
    reserve_slot,
    resumable_run,
)
from ainews.web import queries
from ainews.web.i18n import LANGUAGES, note_text, strings
from ainews.web.views import (
    build_advice,
    count_enabled_sources,
    format_step_duration,
    get_templates,
    language_of,
    remember_preferences,
    shell_context,
)

log = logging.getLogger(__name__)
router = APIRouter()

# The slot itself is taken and released by `pipeline.runner`, which is where every
# entry point passes; this route only *reserves* it, because it has to answer the
# request before the run it schedules has begun. A second press while a run is in
# flight is refused, not queued.
# The event loop keeps only a weak reference to a task, so a fire-and-forget
# digest can be garbage-collected mid-run. Holding it here is what stops that.
_tasks: set[asyncio.Task[None]] = set()


def is_running() -> bool:
    return digest_in_flight()


async def _guarded(language: str, models: tuple[str, str], token: str) -> None:
    """Run under the slot this route already reserved.

    The `finally` is not the release - `run_digest` holds and releases the token
    itself. It is the one case that function cannot cover: a task cancelled
    between `create_task` and its first line never reaches the runner at all, and
    would otherwise leave the slot held forever. `release_slot` is idempotent.
    """
    from ainews.pipeline.runner import run_digest

    try:
        await run_digest(
            language=language,  # type: ignore[arg-type]
            model_summarize=models[0],
            model_rank=models[1],
            reserved_token=token,
        )
    except Exception:
        log.exception("manual run failed")
    finally:
        release_slot(token)


async def _guarded_resume(run_id: str, token: str) -> None:
    from ainews.pipeline.runner import run_digest

    try:
        await run_digest(resume=run_id, reserved_token=token)
    except Exception:
        log.exception("resumed run %s failed", run_id)
    finally:
        release_slot(token)


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

    Two more facts since 2026-09-08, both only when the question is open. How
    many articles are waiting to be summarised, because a press is a delta and
    a delta of two stories is a bulletin of two stories - the reader should see
    that number before paying for it, not after. And, when the last run failed
    and its checkpoint can carry on, the offer to resume it: the summaries it
    paid for are in the checkpoint, and finishing costs the nodes after the one
    that failed. It is an offer inside the one press rather than a second
    button, which is where ADR 0015 put the app's whole paid surface.
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
        "n_candidates": await queries.count_candidates(session) if asking else None,
        "resume": await resumable_run(session, settings) if asking else None,
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
            # The one eval number the interface can move, and until now the one
            # nothing in the interface mentioned. Beside the spend because that
            # is the strip of figures the machine keeps about itself, and this
            # page is where those live (ADR 0009, 0013).
            "verdicts": await queries.verdict_progress(session),
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

    # Reserve before creating the task, not inside it: `create_task` only
    # schedules, so a second press arriving before the task's first line would
    # find the slot still free and start a second - paid - digest. The run holds
    # the same token from there on.
    token = f"manual:{produce}:{models[0]}:{models[1]}"
    if not await reserve_slot(token, "digest"):
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


@router.post("/runs/resume", response_class=HTMLResponse)
async def resume_run(
    request: Request,
    run: str,
    lang: str | None = None,
    session: AsyncSession = Depends(db_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Finish a failed run from its checkpoint.

    The same shape as `/runs/start` - the claim, the task, the status line -
    and the same two refusals. One more of its own: the run named has to be the
    one the question offered, the most recent digest and a failed one, so a
    stale fragment cannot resume a run that a later press has already
    overtaken. The language and the models are not asked, because they are in
    the checkpoint.
    """
    language = _valid(lang, language_of(request))
    t = strings(language)  # type: ignore[arg-type]

    if not settings.llm_configured:
        return HTMLResponse(f'<span class="bad">{t["no_key"]}</span>')
    offered = await resumable_run(session, settings)
    if offered is None or offered.run.id != run:
        return HTMLResponse(f'<span class="bad">{t["resume_gone"]}</span>')

    token = f"resume:{run}"
    if not await reserve_slot(token, "digest"):
        return HTMLResponse(t["busy"])
    task = asyncio.create_task(_guarded_resume(run, token))
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
    language = _valid(lang, language_of(request))
    t = strings(language)  # type: ignore[arg-type]

    if is_running():
        return HTMLResponse(
            f'<span hx-get="/runs/status?lang={language}" hx-trigger="every 3s" '
            f'hx-swap="outerHTML">{t["running"]}</span>'
        )

    # Every bulletin run, not just the pressed ones. This read `kind == "manual"`
    # until ADR 0026 collapsed the kinds, so a resumed run or one started at a
    # terminal never reported its error on the strip that is polling for it.
    latest = (await session.execute(bulletin_runs().limit(1))).scalar_one_or_none()

    label = t["status_error"] if latest is not None and latest.status == "error" else ""
    # The run is over, so the progress line stops and the page shows the result.
    return HTMLResponse(
        f'<span class="{"bad" if label else ""}">{label}</span>'
        "<script>document.getElementById('bar').classList.remove('is-running');"
        "setTimeout(() => location.reload(), 400);</script>"
    )


@router.get("/runs/verdicts", response_class=HTMLResponse)
async def verdicts_page(
    request: Request,
    session: AsyncSession = Depends(db_session),
) -> HTMLResponse:
    """The labels themselves, under the count that reports them.

    `/runs` states how many summaries carry a verdict; this is the list behind
    that number, and it exists for one step that had no screen: PLAN-EVALS E5
    rewrites the judge prompt "from the `wrong` notes", and until now the only
    way to read a note was to open the story it was written on - or SQL. A
    reader's sentence was being stored and never read back, which is a loop
    with a missing half rather than a small omission.

    A sub-page and not a sixth rail link, for the same reason `/runs/<id>` is
    one: it is opened from the figure it explains, by someone who is already
    looking at that figure, a few times a year. Nothing here is a measurement
    being made - `evals/` is not imported and no call is made (ADR 0019 §2,
    ADR 0023). It reads three tables the web layer already owns.

    The third is `eval_results`, since 2026-09-08: the summaries the grounding
    judge failed, with the sentence it could not support, each with the two
    words under it. The judge's failures are the most informative thing the
    evaluation layer produces and they had been printed once at a terminal and
    filed; a reader who answers one of them here gives the label that measures
    the judge's precision directly, which is the number a one-reader tool acts
    on. Ten of those carry more than a hundred `ok`s on random stories.
    """
    language = language_of(request)
    context = await shell_context(request, session, language, page="runs")
    findings = await queries.judge_findings(session)
    context.update(
        {
            "labels": await queries.labelled_stories(session),
            "verdicts": await queries.verdict_progress(session),
            "findings": findings,
            "n_answered": sum(1 for f in findings if f.verdict is not None),
        }
    )
    response = get_templates().TemplateResponse(request, "verdicts.html", context)
    remember_preferences(request, response, language)
    return response


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
                {
                    "step": s,
                    "share": (s.duration_seconds or 0.0) / span if span else 0.0,
                    # The note is written here and not in the template because
                    # the language belongs to the request: a node records a key
                    # and its numbers, and this is the only layer that knows
                    # which dictionary to read them through.
                    "note": note_text(t, s.detail),
                    # A node's span at a node's scale, and in the reader's
                    # notation: `format_duration` is the run's clock and reads
                    # four of the six nodes in a digest as "0:00".
                    "span": format_step_duration(t, s.duration_seconds),
                }
                for s in steps
            ],
            "tokens": run.tokens_in + run.tokens_out,
            # Which models actually ran, which until now nothing on screen could
            # say: ADR 0020 made the model a choice at the press, and `runs` has
            # no column for it - `run_steps` is the first place it is written
            # down. Short names, because the tier is the whole question and the
            # ids differ in exactly that segment.
            # `t.get`, not `t[...]`, for the same reason the template falls back
            # to the bare node name: `run_steps` is history, so a node renamed
            # later leaves rows whose label no longer exists, and an old run must
            # not 500 on a missing translation.
            "models": [
                (t.get("step_" + s.node, s.node), s.model.rsplit("-", 1)[-1])
                for s in steps
                if s.model
            ],
        }
    )
    response = get_templates().TemplateResponse(request, "run_detail.html", context)
    remember_preferences(request, response, language)
    return response
