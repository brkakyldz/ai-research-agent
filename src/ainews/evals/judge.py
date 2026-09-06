"""The sampled grounding judge: `ainews eval judge` (PLAN-EVALS E3.2-E3.4).

One question, binary, about grounding only: is every factual claim in the
summary and the why-it-matters line supported by the article text the
summariser was shown? Nothing about style, length, importance or language -
Turkish quality is judged by the reader, never by a model.

Sampled, not exhaustive: twelve summaries a day on the judge's tier costs about
$2 a month, where judging all ninety would cost six times the product. The
sample is seeded so a re-run judges the same twelve, the cost is estimated from
body length before the first call and the command refuses above `--max-cost`,
and every judged summary becomes an `EvalResult` row - eval spend is on the
record like product spend.

Calibration (`--labelled`) judges every summary that carries a reader's
`Verdict` and reports TPR and TNR separately, never one accuracy figure: a
judge that passes everything scores 90% accuracy on a mostly-right digest and
catches nothing.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings, get_settings
from ainews.db import Article, EvalResult, Source, Summary, Verdict
from ainews.pipeline.llm import estimate_cost, resolve_model, usage_from_message
from ainews.pipeline.llm import judge as judge_model
from ainews.pipeline.nodes.summarize import MAX_BODY_CHARS
from ainews.pipeline.prompts import load_prompt

log = logging.getLogger(__name__)

DEFAULT_SAMPLE = 12
DEFAULT_MAX_COST = 0.10
# Enough labels per class before the calibration numbers mean anything.
MIN_LABELS_PER_CLASS = 30
# Rough token estimate for the cost guard: four characters a token, and a
# short structured answer back.
CHARS_PER_TOKEN = 4
OUTPUT_TOKENS_GUESS = 80


class GroundingVerdict(BaseModel):
    """What the judge must return for one summary."""

    passed: bool = Field(description="True only if every claim is supported by the article text.")
    unsupported_claim: str | None = Field(
        default=None,
        description="The first unsupported claim, quoted from the summary. Empty when passed.",
    )


class CostGuard(RuntimeError):
    """Raised before the first call when the estimate is over the cap."""

    def __init__(self, estimate: float, cap: float, n: int) -> None:
        super().__init__(
            f"judging {n} summaries is estimated at ${estimate:.4f}, over --max-cost ${cap:.2f}; "
            "raise the cap or lower --sample"
        )
        self.estimate = estimate
        self.cap = cap


@dataclass(slots=True)
class Candidate:
    summary_id: int
    run_id: str
    article_id: int
    source: str
    title: str
    body: str
    summary: str
    why_it_matters: str
    human_verdict: str | None = None


@dataclass(slots=True)
class Outcome:
    candidate: Candidate
    passed: bool | None
    claim: str | None
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None


@dataclass(slots=True)
class JudgeReport:
    run_id: str
    model: str
    outcomes: list[Outcome] = field(default_factory=list)
    est_cost_usd: float = 0.0

    @property
    def n_passed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed is True)

    @property
    def n_failed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed is False)

    @property
    def n_unparsed(self) -> int:
        return sum(1 for o in self.outcomes if o.passed is None)


# -- loading ------------------------------------------------------------------


def _candidate(
    summary: Summary, article: Article, source: Source, verdict: str | None
) -> Candidate:
    return Candidate(
        summary_id=summary.id,
        run_id=summary.run_id,
        article_id=article.id,
        source=source.name,
        title=article.title,
        # The judge reads what the summariser read - the same cut-off - so a
        # claim from past the cut-off is unsupported here too.
        body=(article.body_text or "")[:MAX_BODY_CHARS],
        summary=summary.summary,
        why_it_matters=summary.why_it_matters,
        human_verdict=verdict,
    )


async def load_candidates(session: AsyncSession, run_id: str) -> list[Candidate]:
    """Every summary of the run that has a body to be judged against."""
    rows = (
        await session.execute(
            select(Summary, Article, Source)
            .join(Article, Article.id == Summary.article_id)
            .join(Source, Source.id == Article.source_id)
            .where(Summary.run_id == run_id)
            .where(Article.body_text.isnot(None))
            .where(Article.body_text != "")
            .order_by(Summary.id)
        )
    ).all()
    return [_candidate(s, a, src, None) for s, a, src in rows]


async def load_labelled(session: AsyncSession) -> list[Candidate]:
    """Every summary, in any run, that carries a reader's verdict."""
    rows = (
        await session.execute(
            select(Summary, Article, Source, Verdict)
            .join(Article, Article.id == Summary.article_id)
            .join(Source, Source.id == Article.source_id)
            .join(Verdict, Verdict.summary_id == Summary.id)
            .where(Article.body_text.isnot(None))
            .order_by(Summary.id)
        )
    ).all()
    return [_candidate(s, a, src, v.verdict) for s, a, src, v in rows]


def choose_sample(candidates: list[Candidate], sample: int, seed: int) -> list[Candidate]:
    """`sample` candidates, the same ones for the same seed and the same run."""
    ordered = sorted(candidates, key=lambda c: c.summary_id)
    if sample >= len(ordered):
        return ordered
    chosen = random.Random(seed).sample(ordered, sample)
    return sorted(chosen, key=lambda c: c.summary_id)


# -- the call -----------------------------------------------------------------


def build_prompt(candidate: Candidate) -> str:
    return load_prompt("judge_grounding", "en").format(
        source=candidate.source,
        title=candidate.title,
        body=candidate.body or "(no body text)",
        summary=candidate.summary,
        why_it_matters=candidate.why_it_matters,
    )


def estimate_judge_cost(candidates: list[Candidate], model: str) -> float:
    tokens_in = sum(len(build_prompt(c)) // CHARS_PER_TOKEN for c in candidates)
    return estimate_cost(model, tokens_in, OUTPUT_TOKENS_GUESS * len(candidates))


async def judge_one(candidate: Candidate, settings: Settings, model: str | None = None) -> Outcome:
    """Judge one summary. Never raises: a failure is an outcome with `error`."""
    try:
        client = judge_model(settings, model).with_structured_output(
            GroundingVerdict, include_raw=True
        )
        response: Any = await client.ainvoke(build_prompt(candidate))
    except Exception as exc:
        log.warning("judge failed for summary %d: %s", candidate.summary_id, exc)
        return Outcome(candidate, None, None, error=f"{type(exc).__name__}: {exc}"[:300])

    parsed = response.get("parsed") if isinstance(response, dict) else None
    usage = usage_from_message(response.get("raw") if isinstance(response, dict) else None)
    if parsed is None:
        return Outcome(
            candidate, None, None, usage.tokens_in, usage.tokens_out, error="unparsable output"
        )
    claim = (parsed.unsupported_claim or "").strip() or None
    return Outcome(candidate, bool(parsed.passed), claim, usage.tokens_in, usage.tokens_out)


async def judge_candidates(
    session: AsyncSession,
    candidates: list[Candidate],
    *,
    max_cost: float,
    settings: Settings | None = None,
    model: str | None = None,
) -> list[Outcome]:
    """Judge each candidate, write one `EvalResult` per outcome, commit once.

    The model is resolved here rather than read from settings, so the name the
    cost guard prices, the name the calls go to and the name written on every
    `EvalResult` row are one value (ADR 0020). Judging the same run twice on two
    tiers is a legitimate thing to want; a record that could not tell the two
    apart afterwards would not be one.
    """
    settings = settings or get_settings()
    model = resolve_model(model, settings.openai_model_judge)
    estimate = estimate_judge_cost(candidates, model)
    if estimate > max_cost:
        raise CostGuard(estimate, max_cost, len(candidates))

    outcomes: list[Outcome] = []
    for candidate in candidates:
        outcome = await judge_one(candidate, settings, model)
        outcomes.append(outcome)
        session.add(
            EvalResult(
                run_id=candidate.run_id,
                summary_id=candidate.summary_id,
                kind="grounding",
                passed=outcome.passed,
                detail=outcome.claim or outcome.error,
                model=model,
                tokens_in=outcome.tokens_in,
                tokens_out=outcome.tokens_out,
                est_cost_usd=estimate_cost(model, outcome.tokens_in, outcome.tokens_out),
            )
        )
    await session.commit()
    return outcomes


async def judge_run(
    session: AsyncSession,
    run_id: str,
    *,
    sample: int = DEFAULT_SAMPLE,
    seed: int = 0,
    max_cost: float = DEFAULT_MAX_COST,
    settings: Settings | None = None,
    model: str | None = None,
) -> JudgeReport:
    settings = settings or get_settings()
    model = resolve_model(model, settings.openai_model_judge)
    chosen = choose_sample(await load_candidates(session, run_id), sample, seed)
    outcomes = await judge_candidates(
        session, chosen, max_cost=max_cost, settings=settings, model=model
    )
    cost = sum(estimate_cost(model, o.tokens_in, o.tokens_out) for o in outcomes)
    return JudgeReport(run_id, model, outcomes, cost)


# -- calibration --------------------------------------------------------------


@dataclass(slots=True)
class Calibration:
    """Judge against reader, with the positive class being *wrong*.

    TPR: of the summaries the reader called wrong, the share the judge failed.
    TNR: of the summaries the reader called ok, the share the judge passed.
    Reported separately, with the count behind each, and never folded into one
    accuracy figure.
    """

    wrong_caught: int = 0  # reader wrong, judge failed
    wrong_missed: int = 0  # reader wrong, judge passed
    ok_passed: int = 0  # reader ok, judge passed
    ok_failed: int = 0  # reader ok, judge failed
    unparsed: int = 0

    @property
    def n_wrong(self) -> int:
        return self.wrong_caught + self.wrong_missed

    @property
    def n_ok(self) -> int:
        return self.ok_passed + self.ok_failed

    @property
    def tpr(self) -> float | None:
        return self.wrong_caught / self.n_wrong if self.n_wrong else None

    @property
    def tnr(self) -> float | None:
        return self.ok_passed / self.n_ok if self.n_ok else None

    @property
    def trusted(self) -> bool:
        return min(self.n_wrong, self.n_ok) >= MIN_LABELS_PER_CLASS


def calibrate(pairs: list[tuple[str, bool | None]]) -> Calibration:
    """`(human_verdict, judge_passed)` pairs in, the 2x2 out."""
    table = Calibration()
    for human, judged in pairs:
        if judged is None:
            table.unparsed += 1
        elif human == "wrong":
            if judged:
                table.wrong_missed += 1
            else:
                table.wrong_caught += 1
        elif judged:
            table.ok_passed += 1
        else:
            table.ok_failed += 1
    return table


def format_calibration(table: Calibration) -> str:
    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.0%}"

    lines = [
        "                judge FAIL   judge PASS",
        f"reader wrong    {table.wrong_caught:>10}   {table.wrong_missed:>10}",
        f"reader ok       {table.ok_failed:>10}   {table.ok_passed:>10}",
        "",
        f"TPR (wrong caught)  {pct(table.tpr)}  on {table.n_wrong} labelled wrong",
        f"TNR (ok passed)     {pct(table.tnr)}  on {table.n_ok} labelled ok",
    ]
    if table.unparsed:
        lines.append(f"{table.unparsed} judged but unparsable, not counted")
    if not table.trusted:
        lines.append(
            f"not enough labels to trust: {MIN_LABELS_PER_CLASS} per class needed, "
            f"have {table.n_wrong} wrong / {table.n_ok} ok"
        )
    return "\n".join(lines)


async def judge_labelled(
    session: AsyncSession,
    *,
    max_cost: float = DEFAULT_MAX_COST,
    settings: Settings | None = None,
    model: str | None = None,
) -> tuple[Calibration, list[Outcome]]:
    settings = settings or get_settings()
    labelled = await load_labelled(session)
    outcomes = await judge_candidates(
        session, labelled, max_cost=max_cost, settings=settings, model=model
    )
    pairs = [(o.candidate.human_verdict or "ok", o.passed) for o in outcomes]
    return calibrate(pairs), outcomes
