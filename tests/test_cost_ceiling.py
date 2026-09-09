"""The guard on the one press that spends money.

ADR 0020 declined a ceiling: the price line under the button was the whole
guard, which holds while the only person who can press it wrote that line. It
does not hold for someone running this with their own key, and the first press
on a fresh installation is the worst case - nothing is collected yet, so a
pre-flight count says zero, `collect` then brings in a week of backlog, and the
press that most needs a ceiling is the one a check at the button waves through.

So the guard sits after `dedupe`, where the real number is known and before
anything has been spent.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings
from ainews.db import Article, Run, Source, Summary
from ainews.pipeline import graph as graph_module
from ainews.pipeline.pricing import (
    RANK_TOKENS_PER_CANDIDATE,
    SUMMARIZE_TOKENS_PER_ARTICLE,
    CostCeiling,
    estimate_cost,
    estimate_digest_cost,
)


def test_the_estimate_prices_each_node_at_the_model_that_will_run_it() -> None:
    """Since ADR 0020 the two may differ by an order of magnitude, and one
    blended number would misprice both halves of the press."""
    n = 20
    summarize_in, summarize_out = SUMMARIZE_TOKENS_PER_ARTICLE
    rank_in, rank_out = RANK_TOKENS_PER_CANDIDATE

    estimate = estimate_digest_cost(n, "gpt-5.6-luna", "gpt-6-astra")
    expected = estimate_cost("gpt-5.6-luna", n * summarize_in, n * summarize_out) + estimate_cost(
        "gpt-6-astra", n * rank_in, n * rank_out
    )

    assert estimate == pytest.approx(expected)
    # Summarising cheaply and ranking expensively is not the same press as
    # doing both cheaply, and the estimate has to be able to tell them apart.
    assert estimate > estimate_digest_cost(n, "gpt-5.6-luna", "gpt-5.6-luna")


def test_an_ordinary_press_is_cents_and_the_backlog_case_is_dollars() -> None:
    """The two numbers the ceiling sits between, from the audit that asked for
    it: twenty candidates on the cheap tier against a week's backlog on the
    expensive one."""
    ordinary = estimate_digest_cost(20, "gpt-5.6-luna", "gpt-5.6-luna")
    backlog = estimate_digest_cost(130, "gpt-6-astra", "gpt-6-astra")

    assert ordinary < 0.10
    assert backlog > 1.00


async def test_a_press_over_the_ceiling_is_refused_before_it_spends(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`dedupe` raises, so `enrich` never runs and no branch is ever sent.

    Enrichment is the first node that spends anything - Tavily credits before
    the fan-out spends tokens - so a guard that fires here has cost nothing.
    """
    monkeypatch.setattr(settings, "digest_max_cost_usd", 0.01)
    monkeypatch.setattr(graph_module, "get_settings", lambda: settings)

    with pytest.raises(CostCeiling) as caught:
        graph_module._guard_cost({"model_summarize": "gpt-6-astra"}, 130)

    assert caught.value.n_candidates == 130
    assert caught.value.ceiling == 0.01
    # The message carries the numbers: the run row shows this text verbatim, and
    # "too expensive" alone leaves the reader with nothing to decide on.
    assert "130 candidates" in str(caught.value)
    assert "$0.01" in str(caught.value)


async def test_an_ordinary_day_passes_the_guard(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "digest_max_cost_usd", 1.00)
    monkeypatch.setattr(graph_module, "get_settings", lambda: settings)

    graph_module._guard_cost({}, 20)


async def test_a_ceiling_of_zero_turns_the_guard_off(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator who wants no ceiling should be able to say so, rather than
    setting a number large enough to mean the same thing."""
    monkeypatch.setattr(settings, "digest_max_cost_usd", 0)
    monkeypatch.setattr(graph_module, "get_settings", lambda: settings)

    graph_module._guard_cost({"model_summarize": "gpt-6-astra"}, 100_000)


async def test_the_guard_stops_the_run_at_dedupe_and_closes_it(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: a fresh installation, a week of backlog, the expensive tier.

    The run opens, collects for free, and stops with the estimate on the row.
    Nothing was summarised, so nothing was paid for.
    """
    source = Source(name="TechCrunch", url="https://example.com/feed", weight=1.0)
    session.add(source)
    await session.flush()
    for i in range(40):
        session.add(
            Article(
                source_id=source.id,
                title=f"A distinct headline number {i}",
                url=f"https://example.com/{i}",
                url_canonical=f"https://example.com/{i}",
                body_text="Some body text that is long enough to be usable." * 20,
            )
        )
    run = Run(kind="digest", language="en")
    session.add(run)
    await session.commit()

    monkeypatch.setattr(settings, "digest_max_cost_usd", 0.01)
    monkeypatch.setattr(graph_module, "get_settings", lambda: settings)

    async def _no_collect(*_: object, **__: object) -> object:
        from ainews.pipeline.nodes.collect import CollectStats

        return CollectStats()

    monkeypatch.setattr(graph_module, "collect_articles", _no_collect)

    app = graph_module.build_graph().compile()
    with pytest.raises(CostCeiling):
        await app.ainvoke(
            {
                "run_id": run.id,
                "language": "en",
                "model_summarize": "gpt-6-astra",
                "candidate_ids": [],
                "summaries": [],
                "ranked": [],
                "errors": [],
            },
            config={"recursion_limit": 50},
        )

    assert (await session.execute(Summary.__table__.select())).first() is None
