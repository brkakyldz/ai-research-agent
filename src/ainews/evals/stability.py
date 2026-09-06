"""The rank-stability probe: `ainews eval rank-stability` (PLAN-EVALS E3.5).

The ranker reads a numbered table and answers with numbers. If the order it
answers with depends on the order it was shown, the digest's lead story is an
accident of database order. The probe shuffles the candidate table a few ways,
calls `rank_summaries` for each, and reports mean pairwise Kendall tau over the
returned orders and mean Jaccard of the top-N sets. Two extra rank calls, about
$0.006 a run.

tau is fifteen lines of pairwise concordance and no new dependency. Below 0.6
across runs is the E5 trigger for permutation self-consistency in production.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from itertools import combinations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings, get_settings
from ainews.db import Article, EvalResult, Run, Source, Summary
from ainews.pipeline.llm import estimate_cost
from ainews.pipeline.nodes.rank import FALLBACK_NOTE, rank_summaries
from ainews.pipeline.state import SummaryPayload

DEFAULT_TIMES = 3


def kendall_tau(a: list[int], b: list[int]) -> float:
    """Kendall's tau over the items both orders contain.

    1.0 for identical orders, -1.0 for reversed, and 1.0 by convention when
    fewer than two items are shared (nothing to disagree about).
    """
    shared = [x for x in a if x in set(b)]
    if len(shared) < 2:
        return 1.0
    pos_b = {x: i for i, x in enumerate(b)}
    concordant = discordant = 0
    for x, y in combinations(shared, 2):
        # x precedes y in a; do they keep that order in b?
        if pos_b[x] < pos_b[y]:
            concordant += 1
        else:
            discordant += 1
    return (concordant - discordant) / (concordant + discordant)


def jaccard(a: list[int], b: list[int]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def mean_pairwise(orders: list[list[int]], measure) -> float:  # type: ignore[no-untyped-def]
    pairs = list(combinations(orders, 2))
    if not pairs:
        return 1.0
    return sum(measure(a, b) for a, b in pairs) / len(pairs)


@dataclass(slots=True)
class StabilityReport:
    run_id: str
    times: int
    orders: list[list[int]] = field(default_factory=list)
    tau: float = 1.0
    jaccard: float = 1.0
    n_fallback: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    est_cost_usd: float = 0.0

    def detail(self) -> str:
        return json.dumps(
            {
                "tau": round(self.tau, 4),
                "jaccard": round(self.jaccard, 4),
                "times": self.times,
                "n_fallback": self.n_fallback,
                "orders": self.orders,
            }
        )


async def load_table(
    session: AsyncSession, run_id: str
) -> tuple[list[SummaryPayload], dict[int, tuple[str, float]], str]:
    """The run's summaries as the payloads the ranker saw, plus source meta."""
    run = await session.get(Run, run_id)
    if run is None:
        raise LookupError(f"no run {run_id}")
    rows = (
        await session.execute(
            select(Summary, Article, Source)
            .join(Article, Article.id == Summary.article_id)
            .join(Source, Source.id == Article.source_id)
            .where(Summary.run_id == run_id)
            .order_by(Summary.id)
        )
    ).all()
    payloads: list[SummaryPayload] = []
    meta: dict[int, tuple[str, float]] = {}
    for summary, article, source in rows:
        payloads.append(
            {
                "article_id": article.id,
                "title_local": summary.title_local,
                "summary": summary.summary,
                "why_it_matters": summary.why_it_matters,
                "tags": json.loads(summary.tags_json or "[]"),
                "importance": summary.importance,
                "tokens_in": 0,
                "tokens_out": 0,
            }
        )
        meta[article.id] = (source.name, source.weight)
    return payloads, meta, run.language


async def rank_stability(
    session: AsyncSession,
    run_id: str,
    *,
    times: int = DEFAULT_TIMES,
    seed: int = 0,
    settings: Settings | None = None,
) -> StabilityReport:
    settings = settings or get_settings()
    payloads, meta, language = await load_table(session, run_id)
    report = StabilityReport(run_id=run_id, times=times)
    if not payloads:
        return report

    for i in range(times):
        shuffled = list(payloads)
        random.Random(seed + i).shuffle(shuffled)
        ordered, note, tokens_in, tokens_out = await rank_summaries(
            shuffled, meta, language, settings
        )
        report.orders.append(ordered)
        report.tokens_in += tokens_in
        report.tokens_out += tokens_out
        if note == FALLBACK_NOTE.get(language, ""):
            # `rank_summaries` swallows a failed call and answers with the
            # importance order, which is perfectly stable and says nothing
            # about the model. Counted so the number is not mistaken for one.
            report.n_fallback += 1

    report.tau = mean_pairwise(report.orders, kendall_tau)
    report.jaccard = mean_pairwise(report.orders, jaccard)
    report.est_cost_usd = estimate_cost(settings.openai_model, report.tokens_in, report.tokens_out)

    session.add(
        EvalResult(
            run_id=run_id,
            summary_id=None,
            kind="rank_stability",
            passed=None if report.n_fallback else report.tau >= 0.6,
            detail=report.detail(),
            model=settings.openai_model,
            tokens_in=report.tokens_in,
            tokens_out=report.tokens_out,
            est_cost_usd=report.est_cost_usd,
        )
    )
    await session.commit()
    return report
