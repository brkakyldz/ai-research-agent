"""The rank-stability probe: `ainews eval rank-stability` (PLAN-EVALS E3.5).

The ranker reads a numbered table and answers with numbers. If the order it
answers with depends on the order it was shown, the bulletin's lead story is an
accident of database order.

Production now asks that question of itself: every bulletin is three shuffled
rank calls aggregated by Borda count, and `bulletins.agreement` is how much they
agreed (ADR 0030). What this command adds is a *second opinion at a different
time* - and, unlike the production number, it includes the order that actually
shipped among the readings being compared. A set of shuffles that agree
beautifully with each other and not with the published page is the failure the
old probe could not see, because it never looked at the page.

The measures themselves live in `pipeline/agreement.py`, one implementation for
the number the product computes and the number this checks it against. tau is
fifteen lines of pairwise concordance and no new dependency; below 0.6 across
bulletins is the E5 trigger for permutation self-consistency.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings, get_settings
from ainews.db import Bulletin, BulletinItem, EvalResult
from ainews.pipeline.agreement import (
    chance_corrected_jaccard,
    jaccard,
    kendall_tau,
    mean_pairwise,
)
from ainews.pipeline.nodes.rank import day_pool, previous_headlines, rank_once
from ainews.pipeline.pricing import estimate_cost, resolve_model
from ainews.pipeline.prompts import prompt_version

DEFAULT_TIMES = 3

# The same bar E5 names. Here rather than in the caller because the row this
# writes carries `passed`, and a threshold that lives in the command would make
# two runs of it disagree about what a stored pass meant.
#
# It gates `min(tau, chance-corrected Jaccard)` and not tau alone. Both halves
# are on a scale where indifference is 0.0 and agreement is 1.0, and they fail
# independently: readings can order five stories identically while disagreeing
# about which five belong, and they can pick the same fifteen in three unrelated
# orders. The correction is what stops the pool size doing the work - fifteen of
# twenty-seven scores 0.385 by coin flip.
FLOOR = 0.6


@dataclass(slots=True)
class StabilityReport:
    bulletin_id: int
    times: int
    # Which model was probed. On the report and not only on the row it writes,
    # because the number this produces - tau - is meaningless without it: two
    # tiers disagreeing about a day is not the same finding as one tier
    # disagreeing with itself.
    model: str = ""
    # The published order first, then one per shuffled call. Keeping the
    # published one in the list is the point of the probe: it is what the reader
    # was given, and what the shuffles have to agree with.
    orders: list[list[int]] = field(default_factory=list)
    tau: float = 1.0
    # Raw, so the report can print what fraction of the stories were shared.
    jaccard: float = 1.0
    # How many candidates the readings chose from, which is what says how much
    # of `jaccard` two indifferent readings would have scored anyway.
    pool: int = 0
    n_fallback: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    est_cost_usd: float = 0.0

    @property
    def adjusted_jaccard(self) -> float:
        """`jaccard` with the chance level of this pool taken out of it."""
        if not self.orders:
            return self.jaccard
        kept = round(sum(len(order) for order in self.orders) / len(self.orders))
        return chance_corrected_jaccard(self.jaccard, kept, self.pool)

    @property
    def score(self) -> float:
        """The number the floor is applied to: the weaker of the two measures."""
        return min(self.tau, self.adjusted_jaccard)

    @property
    def passed(self) -> bool | None:
        """`None` when the probe measured nothing it can stand behind."""
        if self.n_fallback or len(self.orders) < 2:
            return None
        return self.score >= FLOOR

    def detail(self) -> str:
        return json.dumps(
            {
                "tau": round(self.tau, 4),
                "jaccard": round(self.jaccard, 4),
                "adjusted_jaccard": round(self.adjusted_jaccard, 4),
                "score": round(self.score, 4),
                "pool": self.pool,
                "times": self.times,
                "n_fallback": self.n_fallback,
                "orders": self.orders,
            }
        )


async def published_order(session: AsyncSession, bulletin_id: int) -> list[int]:
    """The summary ids the reader was given, in the order they were given in."""
    return list(
        (
            await session.execute(
                select(BulletinItem.summary_id)
                .where(BulletinItem.bulletin_id == bulletin_id)
                .order_by(BulletinItem.position)
            )
        ).scalars()
    )


async def rank_stability(
    session: AsyncSession,
    bulletin_id: int,
    *,
    times: int = DEFAULT_TIMES,
    seed: int = 0,
    settings: Settings | None = None,
    model: str | None = None,
) -> StabilityReport:
    settings = settings or get_settings()
    # The probe measures a *model's* agreement with itself, so the model has to
    # be nameable (ADR 0020): asking whether luna ranks stably is a different
    # question from asking it of terra, and the answer is filed against
    # whichever one ran.
    model = resolve_model(model, settings.openai_model)
    bulletin = await session.get(Bulletin, bulletin_id)
    if bulletin is None:
        raise LookupError(f"no bulletin {bulletin_id}")

    report = StabilityReport(bulletin_id=bulletin_id, times=times, model=model)
    candidates = await day_pool(session, bulletin.day, bulletin.language, settings)
    if not candidates:
        return report
    report.pool = len(candidates)
    previous = await previous_headlines(session, bulletin.day, bulletin.language)
    top_n = min(settings.digest_top_n, len(candidates))

    shipped = await published_order(session, bulletin_id)
    if shipped:
        report.orders.append(shipped)

    for i in range(times):
        shuffled = list(candidates)
        random.Random(seed + i).shuffle(shuffled)
        result = await rank_once(
            shuffled,
            bulletin.language,
            top_n=top_n,
            previous=previous,
            settings=settings,
            model=model,
        )
        if result is None:
            # A failed call answers with nothing rather than with an importance
            # order, so there is no perfectly stable non-answer to mistake for a
            # measurement. Counted so the sample size on screen is honest.
            report.n_fallback += 1
            continue
        report.orders.append(result.order)
        report.tokens_in += result.tokens_in
        report.tokens_out += result.tokens_out

    report.tau = mean_pairwise(report.orders, kendall_tau)
    report.jaccard = mean_pairwise(report.orders, jaccard)
    report.est_cost_usd = estimate_cost(model, report.tokens_in, report.tokens_out)

    session.add(
        EvalResult(
            bulletin_id=bulletin_id,
            summary_id=None,
            kind="rank_stability",
            passed=report.passed,
            detail=report.detail(),
            model=model,
            # The rank prompt in the bulletin's own language: this measures how
            # stably *those* instructions are followed, and the two files are
            # translations rather than copies.
            prompt_version=prompt_version("rank", bulletin.language),
            tokens_in=report.tokens_in,
            tokens_out=report.tokens_out,
            est_cost_usd=report.est_cost_usd,
        )
    )
    await session.commit()
    return report
