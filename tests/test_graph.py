"""Graph tests with a fake model.

The point of these is that the wiring is exercised without an API key and without
spending anything: the fan-out really fans out, the reducer really concatenates,
a branch that raises really does not take the run down, and the run row really
gets its numbers.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings
from ainews.db import Article, Run, Source, Summary
from ainews.pipeline import graph as graph_module
from ainews.pipeline.llm import PRICES, estimate_cost, price_for, usage_from_message
from ainews.pipeline.nodes import rank as rank_module
from ainews.pipeline.nodes import summarize as summarize_module
from ainews.pipeline.prompts import available_languages, load_prompt
from ainews.pipeline.state import ArticleSummary, RankedDigest


class FakeMessage:
    def __init__(self, tokens_in: int = 100, tokens_out: int = 40) -> None:
        self.usage_metadata = {"input_tokens": tokens_in, "output_tokens": tokens_out}
        self.response_metadata: dict[str, Any] = {}


class FakeStructuredModel:
    """Stands in for `llm.with_structured_output(Model, include_raw=True)`."""

    def __init__(self, parsed: Any, *, fail_on: str | None = None) -> None:
        self._parsed = parsed
        self._fail_on = fail_on

    async def ainvoke(self, prompt: str, *_: object, **__: object) -> dict[str, Any]:
        if self._fail_on and self._fail_on in prompt:
            raise RuntimeError("simulated upstream 500")
        return {"parsed": self._parsed, "raw": FakeMessage()}


class FakeLLM:
    def __init__(self, parsed: Any, *, fail_on: str | None = None) -> None:
        self._parsed = parsed
        self._fail_on = fail_on

    def with_structured_output(self, _schema: Any, **__: object) -> FakeStructuredModel:
        return FakeStructuredModel(self._parsed, fail_on=self._fail_on)


SUMMARY = ArticleSummary(
    title_local="Yeni bir model duyuruldu",
    summary="Bir sirket yeni bir model yayinladi. Model ucuz. Yeni olan fiyat.",
    why_it_matters="Fiyat rekabeti degisiyor.",
    tags=["openai", "models"],
    importance=4,
)


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summarize_module, "summarizer", lambda *_: FakeLLM(SUMMARY))
    monkeypatch.setattr(
        rank_module,
        "ranker",
        lambda *_: FakeLLM(RankedDigest(editor_note="Ucuz modeller gunu.", order=[2, 1, 3])),
    )


# Deliberately unrelated headlines: the dedupe step is real in these tests and
# three variations on one sentence would - correctly - collapse into one story.
TITLES = [
    "OpenAI cuts the price of its cheapest model",
    "European regulators publish AI Act enforcement guidance",
    "A robotics startup raises 300 million dollars",
    "Hugging Face ships a new dataset viewer",
    "Researchers show a jailbreak that survives fine-tuning",
]


async def _seed_articles(session: AsyncSession, n: int) -> list[int]:
    src = Source(name="Lab", url="https://lab.dev/feed", weight=1.5)
    session.add(src)
    await session.flush()
    ids = []
    for i in range(n):
        art = Article(
            source_id=src.id,
            title=TITLES[i],
            url=f"https://lab.dev/{i}",
            url_canonical=f"https://lab.dev/{i}",
            body_text="Some body text that is long enough to be usable. " * 20,
        )
        session.add(art)
        await session.flush()
        ids.append(art.id)
    await session.commit()
    return ids


# -- prompts ------------------------------------------------------------------


def test_both_languages_have_both_prompts() -> None:
    assert set(available_languages()) == {"en", "tr"}
    for language in ("en", "tr"):
        assert "{body}" in load_prompt("summarize", language)
        assert "{candidates}" in load_prompt("rank", language)
        assert "{top_n}" in load_prompt("rank", language)


def test_the_prompt_carries_the_article(session: AsyncSession) -> None:
    prompt = summarize_module.build_prompt(
        "tr",
        source="OpenAI",
        title="A title",
        url="https://x.dev/a",
        published="2026-09-04",
        body="Body sentence.",
    )
    assert "A title" in prompt
    assert "Body sentence." in prompt
    assert "Türkçe" in prompt


def test_a_missing_body_is_stated_not_faked() -> None:
    prompt = summarize_module.build_prompt(
        "en", source="HN", title="T", url="u", published="p", body=""
    )
    assert "no body text available" in prompt


# -- costing ------------------------------------------------------------------


def test_luna_is_priced_as_the_model_page_says() -> None:
    assert PRICES["gpt-5.6-luna"] == (0.20, 1.20)
    assert price_for("gpt-5.6-luna-2026-08-01") == (0.20, 1.20)


def test_an_unknown_model_is_costed_pessimistically() -> None:
    """A surprise in the billing dashboard is worse than a pessimistic number here."""
    assert price_for("gpt-9-unknown") == (10.00, 50.00)


def test_the_monthly_projection_holds() -> None:
    """PLAN.md promises about $2/month at 5M in and 1M out."""
    monthly = estimate_cost("gpt-5.6-luna", 5_000_000, 1_000_000)
    assert 2.0 <= monthly <= 2.5


def test_usage_is_read_from_the_raw_message() -> None:
    usage = usage_from_message(FakeMessage(1234, 567))
    assert (usage.tokens_in, usage.tokens_out) == (1234, 567)


def test_usage_falls_back_to_legacy_metadata() -> None:
    class Legacy:
        def __init__(self) -> None:
            self.usage_metadata = None
            self.response_metadata = {"token_usage": {"prompt_tokens": 10, "completion_tokens": 3}}

    usage = usage_from_message(Legacy())
    assert (usage.tokens_in, usage.tokens_out) == (10, 3)


def test_usage_falls_through_when_the_modern_keys_are_renamed() -> None:
    """The legacy branch used to be reached only when `usage_metadata` was
    missing. A dict that is present but whose keys have been renamed upstream
    read as zero and returned there, so the fallback that still had the numbers
    was never consulted."""

    class Renamed:
        def __init__(self) -> None:
            self.usage_metadata = {"inputTokens": 900, "outputTokens": 300}
            self.response_metadata = {
                "token_usage": {"prompt_tokens": 900, "completion_tokens": 300}
            }

    usage = usage_from_message(Renamed())
    assert (usage.tokens_in, usage.tokens_out) == (900, 300)


def test_usage_that_cannot_be_read_says_so(caplog: pytest.LogCaptureFixture) -> None:
    """A completed call always spent input tokens, so zero is not a cheap call -
    it is accounting that has stopped working. Silent, it reaches the screen as
    `$0.000` on every row and a month of spend reported as nothing, which is the
    surprise `/runs` exists to prevent."""

    class Blank:
        pass

    with caplog.at_level(logging.WARNING, logger="ainews.pipeline.llm"):
        usage = usage_from_message(Blank())

    assert (usage.tokens_in, usage.tokens_out) == (0, 0)
    assert "no token usage" in caplog.text


# -- the graph ----------------------------------------------------------------


async def test_a_full_run_fans_out_ranks_and_persists(
    session: AsyncSession, settings: Settings, fake_llm: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await _seed_articles(session, 3)
    # Collection and enrichment reach the network; the graph shape is what is
    # under test here, so both are stubbed out.
    monkeypatch.setattr(graph_module, "collect_articles", _no_collect)
    monkeypatch.setattr(graph_module, "enrich_articles", _no_enrich)

    run = Run(kind="manual", language="tr")
    session.add(run)
    await session.commit()

    app = graph_module.build_graph().compile()
    result = await app.ainvoke(
        {
            "run_id": run.id,
            "language": "tr",
            "mode": "manual",
            "candidate_ids": [],
            "summaries": [],
            "ranked": [],
            "errors": [],
        },
        config={"recursion_limit": 50},
    )

    assert len([s for s in result["summaries"] if s["article_id"] > 0]) == 3
    assert [r["rank"] for r in result["ranked"]] == [1, 2, 3]

    stored = list((await session.execute(select(Summary))).scalars())
    assert len(stored) == 3
    assert {s.language for s in stored} == {"tr"}
    assert {s.article_id for s in stored} == set(ids)

    # The ranker answered with candidate numbers 2, 1, 3 against the table it was
    # shown, so the ranks have to follow that table's order rather than the order
    # articles happen to sit in the database.
    shown = [s["article_id"] for s in result["summaries"] if s["article_id"] > 0]
    by_article = {s.article_id: s.rank for s in stored}
    assert by_article[shown[1]] == 1
    assert by_article[shown[0]] == 2
    assert by_article[shown[2]] == 3

    await session.refresh(run)
    assert run.status == "ok"
    assert run.n_summarized == 3
    assert run.editor_note == "Ucuz modeller gunu."
    assert run.tokens_in > 0
    assert run.est_cost_usd > 0


async def test_one_failing_article_does_not_fail_the_run(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising branch would take the whole graph down; it must not."""
    await _seed_articles(session, 3)
    monkeypatch.setattr(graph_module, "collect_articles", _no_collect)
    monkeypatch.setattr(graph_module, "enrich_articles", _no_enrich)
    monkeypatch.setattr(
        summarize_module, "summarizer", lambda *_: FakeLLM(SUMMARY, fail_on="AI Act enforcement")
    )
    monkeypatch.setattr(
        rank_module,
        "ranker",
        lambda *_: FakeLLM(RankedDigest(editor_note="Gun ozeti.", order=[1, 2])),
    )

    run = Run(kind="manual", language="en")
    session.add(run)
    await session.commit()

    app = graph_module.build_graph().compile()
    await app.ainvoke(
        {
            "run_id": run.id,
            "language": "en",
            "mode": "manual",
            "candidate_ids": [],
            "summaries": [],
            "ranked": [],
            "errors": [],
        },
        config={"recursion_limit": 50},
    )

    await session.refresh(run)
    assert run.n_summarized == 2
    assert run.status == "partial"
    assert run.error is not None and "simulated upstream 500" in run.error


async def test_a_run_with_nothing_to_summarise_still_closes(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No candidates is a normal morning, not an error."""
    monkeypatch.setattr(graph_module, "collect_articles", _no_collect)
    monkeypatch.setattr(graph_module, "enrich_articles", _no_enrich)

    run = Run(kind="digest", language="tr")
    session.add(run)
    await session.commit()

    app = graph_module.build_graph().compile()
    await app.ainvoke(
        {
            "run_id": run.id,
            "language": "tr",
            "mode": "digest",
            "candidate_ids": [],
            "summaries": [],
            "ranked": [],
            "errors": [],
        },
        config={"recursion_limit": 50},
    )

    await session.refresh(run)
    assert run.status == "ok"
    assert run.n_summarized == 0
    assert run.finished_at is not None


async def test_rank_tokens_are_priced_at_the_rank_model(
    session: AsyncSession, settings: Settings
) -> None:
    """The two model knobs can differ (ADR 0001); the run's cost has to know
    which tokens went where. Until 2026-09-06 everything was priced as the
    summariser, which was right only by coincidence."""
    from ainews.pipeline.nodes.persist import TOKEN_CARRIER_ID, persist_run

    src = Source(name="Lab", url="https://lab.dev/feed")
    session.add(src)
    await session.flush()
    art = Article(
        source_id=src.id, title="t", url="https://lab.dev/1", url_canonical="https://lab.dev/1"
    )
    run = Run(kind="manual", language="en")
    session.add_all([art, run])
    await session.commit()

    split = settings.model_copy(
        update={"openai_model": "gpt-5.6-terra", "openai_model_summarize": "gpt-5.6-luna"}
    )
    state = {
        "run_id": run.id,
        "language": "en",
        "summaries": [
            {
                "article_id": art.id,
                "title_local": "t",
                "summary": "s",
                "why_it_matters": "w",
                "tags": [],
                "importance": 3,
                "tokens_in": 1000,
                "tokens_out": 100,
            },
            {
                "article_id": TOKEN_CARRIER_ID,
                "title_local": "",
                "summary": "",
                "why_it_matters": "",
                "tags": [],
                "importance": 3,
                "tokens_in": 5000,
                "tokens_out": 300,
            },
        ],
        "ranked": [{"article_id": art.id, "rank": 1}],
        "errors": [],
    }
    await persist_run(session, state, split)  # type: ignore[arg-type]

    await session.refresh(run)
    expected = estimate_cost("gpt-5.6-luna", 1000, 100) + estimate_cost("gpt-5.6-terra", 5000, 300)
    assert run.est_cost_usd == pytest.approx(expected)
    assert run.est_cost_usd > estimate_cost("gpt-5.6-luna", 6000, 400), (
        "priced everything as luna: the old, wrong number"
    )
    assert (run.tokens_in, run.tokens_out) == (6000, 400)


# -- ranking fallbacks --------------------------------------------------------


async def test_ranking_falls_back_to_importance_when_the_model_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The digest ships even when the ranker does not."""

    class Broken:
        def with_structured_output(self, *_: object, **__: object) -> Any:
            raise RuntimeError("no ranker today")

    monkeypatch.setattr(rank_module, "ranker", lambda *_: Broken())

    summaries = [
        {"article_id": 1, "importance": 2, "title_local": "a", "summary": "s"},
        {"article_id": 2, "importance": 5, "title_local": "b", "summary": "s"},
        {"article_id": 3, "importance": 3, "title_local": "c", "summary": "s"},
    ]
    meta = {1: ("A", 1.0), 2: ("B", 1.0), 3: ("C", 1.0)}
    order, note, tin, tout = await rank_module.rank_summaries(summaries, meta, "tr")  # type: ignore[arg-type]

    assert order == [2, 3, 1]
    assert "modelsiz" in note
    assert (tin, tout) == (0, 0)


async def test_out_of_range_positions_from_the_model_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model answers with ordinals; anything outside the table is not trusted."""
    monkeypatch.setattr(
        rank_module,
        "ranker",
        lambda *_: FakeLLM(RankedDigest(editor_note="n", order=[2, 99, 2, 1])),
    )
    summaries = [
        {"article_id": 10, "importance": 3, "title_local": "a", "summary": "s"},
        {"article_id": 20, "importance": 3, "title_local": "b", "summary": "s"},
    ]
    meta = {10: ("A", 1.0), 20: ("B", 1.0)}
    order, _, _, _ = await rank_module.rank_summaries(summaries, meta, "en")  # type: ignore[arg-type]

    assert order == [20, 10], "99 is out of range and the repeated 2 is a duplicate"


# -- stubs --------------------------------------------------------------------


async def _no_collect(session: AsyncSession, settings: Settings | None = None) -> Any:
    from ainews.pipeline.nodes.collect import CollectStats

    return CollectStats()


async def _no_enrich(
    session: AsyncSession, article_ids: list[int], settings: Settings | None = None
) -> Any:
    from ainews.pipeline.nodes.enrich import EnrichStats

    return EnrichStats()
