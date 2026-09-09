"""The model is chosen at the press, not read from the environment (ADR 0020).

Four things have to hold for that to be true rather than merely drawn, and each
of them is one failure that has actually cost money in this project:

1. the choice reaches the paid call - otherwise the picker is decoration;
2. it reaches `persist`, so the run row is priced at what ran - a run row that
   says $0.02 for a $0.20 run is worse than no run row;
3. a name from a URL cannot become an API call, because the outside world types
   whatever it likes into a query string;
4. picking one control does not reset another - the confirmation is four
   choices rebuilt on every click, and a dropped parameter is silent.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ainews.clock import local_day
from ainews.config import Settings
from ainews.db import Article, Run, Source, Summary
from ainews.pipeline.pricing import (
    MODEL_NAMES,
    estimate_cost,
    model_options,
    resolve_model,
)
from ainews.web.app import create_app

LUNA = "gpt-5.6-luna"
TERRA = "gpt-5.6-terra"


# -- the menu -----------------------------------------------------------------


def test_the_menu_is_cheapest_first() -> None:
    """The default is the cheapest tier and a control reads left to right, so
    the fifty-times-dearer one has to be a reach rather than a neighbour."""
    options = model_options()
    assert [choice.id for choice in options] == list(MODEL_NAMES)
    assert options[0].id == LUNA
    assert [o.price_in for o in options] == sorted(o.price_in for o in options)


def test_a_slot_is_labelled_by_the_part_of_the_id_that_differs() -> None:
    assert [choice.short for choice in model_options()] == ["luna", "terra", "sol", "astra"]


def test_a_configured_model_the_table_has_not_heard_of_is_still_drawn() -> None:
    """Otherwise the picker would mark nothing and read as broken on exactly the
    machine whose operator had reached for a model by hand."""
    options = model_options("gpt-7-nova")
    assert options[-1].id == "gpt-7-nova"
    assert options[-1].price_in == 10.00, "priced as the top tier, like any unknown"


def test_an_unknown_name_from_outside_resolves_to_the_default() -> None:
    """A query string is written by whoever is holding the keyboard. An
    unrecognised name must not reach the vendor, and must not be priced at
    `FALLBACK_PRICE` against a model that does not exist."""
    assert resolve_model(TERRA, LUNA) == TERRA
    assert resolve_model("gpt-5.6-luna; DROP TABLE runs", LUNA) == LUNA
    assert resolve_model("", LUNA) == LUNA
    assert resolve_model(None, LUNA) == LUNA
    assert resolve_model("gpt-7-nova", "gpt-7-nova") == "gpt-7-nova", (
        "the environment may name a model this table has not got yet"
    )


# -- the question -------------------------------------------------------------


def test_the_question_offers_both_models_and_marks_the_configured_ones(
    client: TestClient,
) -> None:
    asked = client.get("/runs/confirm?lang=tr").text
    assert asked.count('class="seg seg--model"') == 2, "one row for each paid node"
    assert "Özet modeli" in asked and "Sıralama modeli" in asked
    for name in MODEL_NAMES:
        assert f"ms={name}" in asked and f"mr={name}" in asked
    assert f'hx-post="/runs/start?lang=tr&amp;out=tr&amp;ms={LUNA}&amp;mr={LUNA}"' in asked


def test_the_prices_are_on_the_question_before_the_press(
    client: TestClient,
) -> None:
    """The screen where a reader can pick a tier costing fifty times as much is
    the screen that has to say so. $0.04 of judge calls arriving unannounced in a
    billing dashboard on 2026-09-06 is what this line is for."""
    assert "1M token: özet $0.20/$1.20 · sıralama $0.20/$1.20" in client.get("/runs/confirm").text

    dearer = client.get(f"/runs/confirm?ms={TERRA}").text
    assert "özet $2.00/$12.00 · sıralama $0.20/$1.20" in dearer, "the summariser moved, not both"


def test_choosing_a_model_keeps_the_language_already_chosen(
    client: TestClient,
) -> None:
    """Every slot rebuilds the whole fragment, so a slot that forgot its
    neighbour would quietly undo a choice the reader had already made - and the
    only evidence would be the bulletin arriving in the wrong language."""
    body = client.get(f"/runs/confirm?lang=tr&out=en&ms={TERRA}").text
    assert f'hx-post="/runs/start?lang=tr&amp;out=en&amp;ms={TERRA}&amp;mr={LUNA}"' in body
    assert f"out=en&amp;ms={LUNA}" in body, "the language slots carry the models too"


def test_an_unknown_model_in_the_url_draws_the_default(client: TestClient) -> None:
    body = client.get("/runs/confirm?ms=gpt-9-nonesuch").text
    assert f'hx-post="/runs/start?lang=tr&amp;out=tr&amp;ms={LUNA}' in body


# -- the press ----------------------------------------------------------------


def test_the_press_sends_the_models_it_was_asked_for(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    from ainews.pipeline import runner

    settings.openai_api_key = "sk-test"
    seen: list[tuple[str, str]] = []

    async def _record(model_summarize: str = "", model_rank: str = "", **_: object) -> str:
        seen.append((model_summarize, model_rank))
        return ""

    monkeypatch.setattr(runner, "run_digest", _record)

    with TestClient(create_app(settings)) as client:
        client.post(f"/runs/start?lang=tr&out=tr&ms={TERRA}&mr={LUNA}")

    assert seen == [(TERRA, LUNA)]


def test_a_press_that_names_nothing_runs_the_configured_models(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The CLI's shape, and a fragment left open in a tab since before ADR 0020.
    Neither should error, and neither should reach for an expensive tier."""
    from ainews.pipeline import runner

    settings.openai_api_key = "sk-test"
    seen: list[tuple[str, str]] = []

    async def _record(model_summarize: str = "", model_rank: str = "", **_: object) -> str:
        seen.append((model_summarize, model_rank))
        return ""

    monkeypatch.setattr(runner, "run_digest", _record)

    with TestClient(create_app(settings)) as client:
        client.post("/runs/start?lang=tr")
        client.post("/runs/start?lang=tr&ms=gpt-9-nonesuch")

    assert seen == [(LUNA, LUNA), (LUNA, LUNA)]


# -- the pipeline -------------------------------------------------------------


async def test_the_chosen_model_reaches_the_summarise_call(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, settings: Settings
) -> None:
    """The fan-out carries the model on every `Send`, so a hundred branches are
    one model even if the environment changes underneath them."""
    from ainews.pipeline.nodes import summarize as summarize_module

    src = Source(name="Lab", url="https://lab.dev/feed")
    session.add(src)
    await session.flush()
    art = Article(
        source_id=src.id, title="t", url="https://lab.dev/1", url_canonical="https://lab.dev/1"
    )
    session.add(art)
    await session.commit()

    asked: list[str | None] = []

    def _fake_summarizer(settings: Settings | None = None, model: str | None = None) -> Any:
        asked.append(model)
        raise RuntimeError("far enough: the client was built with a model")

    monkeypatch.setattr(summarize_module, "summarizer", _fake_summarizer)

    task = {"run_id": "r", "language": "en", "article_id": art.id, "model": TERRA}
    result = await summarize_module.summarize_article(task, settings)  # type: ignore[arg-type]

    assert asked == [TERRA]
    assert result["errors"], "a failed branch is an error, not a crash"


async def test_a_send_with_no_model_falls_back_to_the_configured_summariser(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, settings: Settings
) -> None:
    """`SummarizeTask.model` is optional, and a caller that did not choose one
    meant the environment's default."""
    from ainews.pipeline.nodes import summarize as summarize_module

    src = Source(name="Lab", url="https://lab.dev/feed")
    session.add(src)
    await session.flush()
    art = Article(
        source_id=src.id, title="t", url="https://lab.dev/2", url_canonical="https://lab.dev/2"
    )
    session.add(art)
    await session.commit()

    asked: list[str | None] = []

    def _fake_summarizer(settings: Settings | None = None, model: str | None = None) -> Any:
        asked.append(model)
        raise RuntimeError("far enough")

    monkeypatch.setattr(summarize_module, "summarizer", _fake_summarizer)

    task = {"run_id": "r", "language": "en", "article_id": art.id}
    await summarize_module.summarize_article(task, settings)  # type: ignore[arg-type]

    assert asked == [settings.openai_model_summarize]


async def test_the_run_row_is_priced_at_the_models_that_ran(
    session: AsyncSession, settings: Settings
) -> None:
    """The state's models win over the environment's.

    This is the failure mode ADR 0020 has to avoid: a reader presses `terra`,
    pays ten times as much, and the run row - reading `settings` - reports the
    luna price. The cost column is the only record of what a press cost, and the
    press is now the thing that decides it.
    """
    from ainews.pipeline.nodes.persist import persist_run

    src = Source(name="Lab", url="https://lab.dev/feed")
    session.add(src)
    await session.flush()
    art = Article(
        source_id=src.id, title="t", url="https://lab.dev/3", url_canonical="https://lab.dev/3"
    )
    run = Run(kind="digest", language="en")
    session.add_all([art, run])
    await session.commit()

    summary = Summary(
        article_id=art.id,
        language="en",
        title_local="t",
        summary="s",
        why_it_matters="w",
        importance=3,
        model=TERRA,
        tokens_in=1000,
        tokens_out=100,
        est_cost_usd=estimate_cost(TERRA, 1000, 100),
    )
    session.add(summary)
    await session.commit()

    state = {
        "run_id": run.id,
        "language": "en",
        "day": local_day(),
        # Both settings say luna; the run says otherwise and the run is right.
        "model_summarize": TERRA,
        "model_rank": LUNA,
        "summaries": [
            {
                "summary_id": summary.id,
                "article_id": art.id,
                "tokens_in": 1000,
                "tokens_out": 100,
                "est_cost_usd": summary.est_cost_usd,
            }
        ],
        "rank_usage": {"tokens_in": 5000, "tokens_out": 300},
        "ranked": [{"summary_id": summary.id, "position": 1, "tier": "lead", "reason": ""}],
        "errors": [],
    }
    await persist_run(session, state, settings)  # type: ignore[arg-type]

    await session.refresh(run)
    expected = estimate_cost(TERRA, 1000, 100) + estimate_cost(LUNA, 5000, 300)
    assert run.est_cost_usd == pytest.approx(expected)
    assert run.est_cost_usd > estimate_cost(LUNA, 6000, 400), "priced as the environment: the lie"


async def test_run_digest_resolves_both_models_before_it_opens_the_run(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, engine: AsyncEngine
) -> None:
    """Resolved once, in the runner, so every node downstream reads a name."""
    from ainews.pipeline import runner as runner_module

    seen: dict[str, Any] = {}

    class _FakeApp:
        async def ainvoke(self, initial: Any, config: Any, **_: Any) -> Any:
            seen.update(initial)
            return initial

    monkeypatch.setattr(
        runner_module, "build_graph", lambda: SimpleNamespace(compile=lambda **_: _FakeApp())
    )

    await runner_module.run_digest(
        language="en", settings=settings, model_summarize=TERRA, model_rank="nope"
    )

    assert seen["model_summarize"] == TERRA
    assert seen["model_rank"] == LUNA, "an unusable name is the default, not an error"


# -- the terminal -------------------------------------------------------------


def test_the_cli_offers_the_same_two_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    """A press and a terminal run have to be makeable identical, or the
    dashboard is only reproducible by eye rather than by argument."""
    from ainews import cli as cli_module

    seen: list[tuple[str | None, str | None]] = []

    async def _fake_digest(
        language: str | None,
        model_summarize: str | None = None,
        model_rank: str | None = None,
        resume: str | None = None,
    ) -> int:
        seen.append((model_summarize, model_rank))
        return 0

    monkeypatch.setattr(cli_module, "_digest", _fake_digest)
    assert cli_module.main(["digest", "--model-summarize", TERRA, "--model-rank", LUNA]) == 0
    assert seen == [(TERRA, LUNA)]


def test_the_cli_refuses_a_model_it_has_no_price_for() -> None:
    """`argparse` answers a typo with the list, which is the right answer for a
    terminal - unlike a URL, where the same typo quietly gets the default."""
    from ainews.cli import main

    with pytest.raises(SystemExit):
        main(["digest", "--model-summarize", "not-a-model"])
