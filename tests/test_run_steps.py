"""What each node did, and the page that reads it (ADR 0022).

The point of these is that the breakdown agrees with the run row it sits under.
A steps table whose costs did not add up to `runs.est_cost_usd`, or whose
fan-out row claimed a span the run did not have, would be worse than no
breakdown: it would be a second set of numbers about the same two minutes.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ainews.config import Settings
from ainews.db import Run, RunStep
from ainews.pipeline import graph as graph_module
from ainews.pipeline import steps as steps_module
from ainews.pipeline.nodes import rank as rank_module
from ainews.pipeline.nodes import summarize as summarize_module
from ainews.pipeline.state import RankedDigest
from ainews.pipeline.steps import step
from ainews.web.app import create_app
from test_graph import SUMMARY, FakeLLM, _no_collect, _no_enrich, _seed_articles

INITIAL: dict[str, Any] = {
    "language": "tr",
    "mode": "manual",
    "candidate_ids": [],
    "summaries": [],
    "ranked": [],
    "errors": [],
}


@pytest.fixture
def client(settings: Settings, engine: AsyncEngine) -> TestClient:
    return TestClient(create_app(settings))


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both paid nodes, answered without a key. A fixture cannot be imported
    from another test module, so this restates `test_graph`'s - the fakes it is
    built from are shared, which is the half worth sharing."""
    monkeypatch.setattr(summarize_module, "summarizer", lambda *_: FakeLLM(SUMMARY))
    monkeypatch.setattr(
        rank_module,
        "ranker",
        lambda *_: FakeLLM(RankedDigest(editor_note="Ucuz modeller gunu.", order=[2, 1, 3])),
    )


async def _rows(session: AsyncSession, run_id: str) -> list[RunStep]:
    return list(
        (
            await session.execute(
                select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.started_at)
            )
        ).scalars()
    )


async def _run_the_graph(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, *, n: int = 3, language: str = "tr"
) -> Run:
    monkeypatch.setattr(graph_module, "collect_articles", _no_collect)
    monkeypatch.setattr(graph_module, "enrich_articles", _no_enrich)
    if n:
        await _seed_articles(session, n)
    run = Run(kind="manual", language=language)
    session.add(run)
    await session.commit()
    app = graph_module.build_graph().compile()
    await app.ainvoke(
        {
            **INITIAL,
            "run_id": run.id,
            "language": language,
            "model_summarize": "gpt-5.6-luna",
            "model_rank": "gpt-5.6-terra",
        },
        config={"recursion_limit": 50},
    )
    return run


# -- recording ----------------------------------------------------------------


async def test_a_full_run_leaves_one_row_per_node_in_order(
    session: AsyncSession, settings: Settings, fake_llm: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await _run_the_graph(session, monkeypatch)
    rows = await _rows(session, run.id)

    # Ordered by the clock, which has to put the fan-out - whose row is written
    # last, from between its neighbours' timestamps - in its place in the graph.
    assert [r.node for r in rows] == [
        "collect",
        "dedupe",
        "enrich",
        "summarize",
        "rank",
        "persist",
    ]
    assert all(r.finished_at is not None for r in rows)
    assert all(r.status == "ok" for r in rows)


async def test_the_fan_out_span_sits_between_its_neighbours(
    session: AsyncSession, settings: Settings, fake_llm: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one node that cannot time itself is timed from the outside."""
    run = await _run_the_graph(session, monkeypatch)
    by_node = {r.node: r for r in await _rows(session, run.id)}

    assert by_node["summarize"].started_at == by_node["enrich"].finished_at
    assert by_node["summarize"].finished_at == by_node["rank"].started_at
    assert by_node["summarize"].duration_seconds >= 0


async def test_the_steps_costs_add_up_to_the_runs_cost(
    session: AsyncSession, settings: Settings, fake_llm: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two models, two rows, one total - the bug ADR 0020 was written against
    would show here as a breakdown that disagrees with the row above it."""
    run = await _run_the_graph(session, monkeypatch)
    by_node = {r.node: r for r in await _rows(session, run.id)}
    await session.refresh(run)

    assert by_node["summarize"].model == "gpt-5.6-luna"
    assert by_node["rank"].model == "gpt-5.6-terra"
    total = sum(r.est_cost_usd for r in by_node.values())
    assert total == pytest.approx(run.est_cost_usd)
    # The four unpaid nodes must not be quietly priced at anything.
    assert all(by_node[n].est_cost_usd == 0.0 for n in ("collect", "dedupe", "enrich", "persist"))


async def test_the_counts_are_the_nodes_own(
    session: AsyncSession, settings: Settings, fake_llm: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await _run_the_graph(session, monkeypatch)
    by_node = {r.node: r for r in await _rows(session, run.id)}

    assert by_node["dedupe"].n_in == 3 and by_node["dedupe"].n_out == 3
    assert by_node["summarize"].n_in == 3 and by_node["summarize"].n_out == 3
    assert by_node["rank"].n_in == 3 and by_node["rank"].n_out == 3
    # The carrier payload the rank node rides its tokens back on is not a summary.
    assert by_node["persist"].n_in == 3


async def test_a_partly_failed_fan_out_says_so(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        summarize_module, "summarizer", lambda *_: FakeLLM(SUMMARY, fail_on="AI Act enforcement")
    )
    monkeypatch.setattr(
        rank_module,
        "ranker",
        lambda *_: FakeLLM(RankedDigest(editor_note="Gun ozeti.", order=[1, 2])),
    )
    run = await _run_the_graph(session, monkeypatch, language="en")
    by_node = {r.node: r for r in await _rows(session, run.id)}

    assert by_node["summarize"].status == "partial"
    assert by_node["summarize"].n_in == 3
    assert by_node["summarize"].n_out == 2
    assert "1 of 3" in by_node["summarize"].detail


async def test_a_run_with_no_candidates_has_no_fan_out_row(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was summarised, so there is no step to describe - and inventing a
    zero-length one would put a node on screen that never ran."""
    run = await _run_the_graph(session, monkeypatch, n=0)
    nodes = [r.node for r in await _rows(session, run.id)]

    assert "summarize" not in nodes
    assert "rank" not in nodes
    assert nodes == ["collect", "dedupe", "enrich", "persist"]


async def test_a_node_that_raises_still_leaves_its_row(
    session: AsyncSession, settings: Settings, engine: AsyncEngine
) -> None:
    """A run that died with no trace of where it died is what this table is for."""
    run = Run(kind="manual", language="tr")
    session.add(run)
    await session.commit()

    with pytest.raises(RuntimeError, match="upstream 500"):
        async with step(run.id, "rank"):
            raise RuntimeError("simulated upstream 500")

    rows = await _rows(session, run.id)
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert "simulated upstream 500" in rows[0].detail
    assert rows[0].finished_at is not None


async def test_bookkeeping_never_kills_the_run(
    session: AsyncSession, settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An insert that can stop a paid digest is worse than no insert (ADR 0018)."""

    def explode(*_: object, **__: object) -> None:
        raise RuntimeError("the disk is gone")

    monkeypatch.setattr(steps_module, "session_scope", explode)
    run = Run(kind="manual", language="tr")
    session.add(run)
    await session.commit()

    async with step(run.id, "collect") as marker:
        marker.counts(1, 1)

    assert await _rows(session, run.id) == []


# -- the page -----------------------------------------------------------------


async def test_the_run_page_reads_each_node_in_its_own_words(
    client: TestClient,
    session: AsyncSession,
    settings: Settings,
    fake_llm: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = await _run_the_graph(session, monkeypatch)
    body = client.get(f"/runs/{run.id}?lang=en").text

    assert "Summarize" in body and "Rank" in body
    # "In" and "out" are different quantities at every node, so each states its
    # own sentence rather than the table carrying one pair of headings.
    assert "candidates" in body and "kept" in body
    assert "summaries" in body and "chosen" in body
    # Which models wrote it. ADR 0020 made that a choice at the press; this is
    # the first page that can say which choice was taken, because `runs` has no
    # column for it and `run_steps` does.
    assert "luna" in body and "terra" in body


async def test_the_runs_table_links_into_the_run(
    client: TestClient,
    session: AsyncSession,
    settings: Settings,
    fake_llm: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = await _run_the_graph(session, monkeypatch)
    assert f'href="/runs/{run.id}' in client.get("/runs?lang=en").text


def test_an_unknown_run_is_a_404_inside_the_shell(client: TestClient) -> None:
    response = client.get("/runs/deadbeef?lang=en")
    assert response.status_code == 404
    assert "There is no such run" in response.text
    # Not the framework's bare page: the way back has to be on it.
    assert "Back to runs" in response.text


def test_the_literal_run_routes_still_win_over_the_id(client: TestClient) -> None:
    """`{run_id}` matches any segment, so `/runs/status` would be a 404 dressed
    as a run if the routes were declared the other way round."""
    for path in ("/runs/status", "/runs/confirm", "/runs/action"):
        assert client.get(f"{path}?lang=en").status_code == 200
