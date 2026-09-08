"""A failed digest is finished from its checkpoint, not paid for twice.

The graph writes a checkpoint after every superstep, and the case these cover
is the expensive one: a hundred summaries land in the checkpoint, `persist`
raises, and without a resume the next press summarises the same hundred
articles again, because no `Summary` row exists. A
resume re-runs the node that failed and the ones after it, and none before.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ainews.config import Settings
from ainews.db import Run, RunStep, Summary
from ainews.pipeline import graph as graph_module
from ainews.pipeline import runner
from ainews.pipeline.nodes import rank as rank_module
from ainews.pipeline.nodes import summarize as summarize_module
from ainews.pipeline.state import Pick, RankedDigest
from ainews.web.app import create_app
from test_graph import SUMMARY, FakeLLM, _no_collect, _no_enrich, _seed_articles


class CountingLLM(FakeLLM):
    """A summariser that counts how many times it was asked."""

    calls = 0

    def with_structured_output(self, schema: Any, **kwargs: object) -> Any:
        CountingLLM.calls += 1
        return super().with_structured_output(schema, **kwargs)


@pytest.fixture
def paid_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    CountingLLM.calls = 0
    monkeypatch.setattr(summarize_module, "summarizer", lambda *_: CountingLLM(SUMMARY))
    monkeypatch.setattr(
        rank_module,
        "ranker",
        lambda *_: FakeLLM(
            RankedDigest(
                editor_note="Gun.",
                picks=[Pick(number=n, importance=4) for n in (1, 2, 3)],
            )
        ),
    )
    monkeypatch.setattr(graph_module, "collect_articles", _no_collect)
    monkeypatch.setattr(graph_module, "enrich_articles", _no_enrich)


async def _fail_then_resume(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, int]:
    """Run once with `persist` broken, then resume with it mended.

    Returns the run id and how many summarise calls the first run made.
    """
    await _seed_articles(session, 3)
    real_persist = graph_module.persist_run

    async def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(graph_module, "persist_run", broken)
    with pytest.raises(RuntimeError, match="locked"):
        await runner.run_digest(language="tr")

    failed = (await session.execute(select(Run))).scalar_one()
    paid = CountingLLM.calls
    assert failed.status == "error" and "locked" in (failed.error or "")
    assert (await session.execute(select(Summary))).scalars().all() == []
    assert paid == 3, "the summaries were paid for"

    monkeypatch.setattr(graph_module, "persist_run", real_persist)
    return failed.id, paid


async def test_a_run_that_died_at_persist_is_finished_without_a_second_summarise(
    session: AsyncSession, settings: Settings, paid_nodes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, paid = await _fail_then_resume(session, monkeypatch)

    assert await runner.pending_nodes(run_id) == ["persist"]

    resumed = await runner.run_digest(resume=run_id)
    assert resumed == run_id, "the bulletin lands on the id the reader saw fail"

    session.expire_all()
    run = await session.get(Run, run_id)
    assert run is not None and run.status == "ok"
    assert run.n_summarized == 3 and run.error is None
    assert len((await session.execute(select(Summary))).scalars().all()) == 3
    assert CountingLLM.calls == paid, "nothing before the failed node ran again"
    assert await runner.pending_nodes(run_id) == [], "the graph reached END"

    # The record shows both attempts at `persist`: the one that raised and the
    # one that wrote (ADR 0022 keeps a failed node's row).
    steps = (
        await session.execute(
            select(RunStep.node, RunStep.status)
            .where(RunStep.run_id == run_id)
            .order_by(RunStep.started_at)
        )
    ).all()
    assert [s for s in steps if s[0] == "persist"] == [("persist", "error"), ("persist", "ok")]


async def test_a_resume_needs_a_failed_run_with_a_checkpoint(
    session: AsyncSession, settings: Settings, paid_nodes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(LookupError, match="no run"):
        await runner.run_digest(resume="nope")

    run = Run(kind="digest", language="tr", status="ok")
    session.add(run)
    await session.commit()
    with pytest.raises(ValueError, match="only a failed run"):
        await runner.run_digest(resume=run.id)

    run.status = "error"
    await session.commit()
    with pytest.raises(LookupError, match="no checkpoint"):
        await runner.run_digest(resume=run.id)
    assert await runner.resumable_run(session) is None, (
        "a failure with no checkpoint is not offered"
    )


async def test_only_the_most_recent_failure_is_offered(
    session: AsyncSession, settings: Settings, paid_nodes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later press summarises the same candidates; resuming the older run
    would write a second bulletin for a day that already has one."""
    run_id, _ = await _fail_then_resume(session, monkeypatch)
    offered = await runner.resumable_run(session)
    assert offered is not None and (offered.run.id, offered.next_node) == (run_id, "persist")

    later = Run(kind="digest", language="tr", status="ok", n_summarized=1)
    session.add(later)
    await session.commit()
    assert await runner.resumable_run(session) is None


# -- the CLI and the page ------------------------------------------------------


def test_the_cli_resumes_by_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    from ainews import cli as cli_module

    seen: list[Any] = []

    async def _fake_digest(
        language: str | None,
        model_summarize: str | None = None,
        model_rank: str | None = None,
        resume: str | None = None,
    ) -> int:
        seen.append(resume)
        return 0

    monkeypatch.setattr(cli_module, "_digest", _fake_digest)
    assert cli_module.main(["digest", "--resume", "5c60e8"]) == 0
    assert seen == ["5c60e8"]


async def test_the_question_offers_to_finish_the_failed_run(
    client: TestClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside the one press (ADR 0015), naming the node it stopped at."""
    from ainews.web.routes import runs as runs_route

    failed = Run(kind="digest", language="tr", status="error", error="RuntimeError: locked")
    session.add(failed)
    await session.commit()

    async def _offer(*_: Any, **__: Any) -> runner.Resumable:
        return runner.Resumable(run=failed, next_node="persist")

    monkeypatch.setattr(runs_route, "resumable_run", _offer)
    asked = client.get("/runs/confirm?lang=tr").text
    assert "Kaldığı yerden devam et" in asked
    assert "Yazma adımında durdu" in asked, "the node is named in the page's own words"
    assert f'hx-post="/runs/resume?lang=tr&amp;run={failed.id}"' in asked
    assert 'hx-post="/runs/start' in asked, "a fresh run is still on offer beside it"

    assert "devam et" not in client.get("/runs/action?lang=tr").text, "only inside the question"


async def test_the_resume_press_starts_the_run_it_was_offered_and_no_other(
    session: AsyncSession, settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ainews.web.routes import runs as runs_route

    settings.openai_api_key = "sk-test"
    failed = Run(kind="digest", language="tr", status="error", error="x")
    session.add(failed)
    await session.commit()
    resumed: list[str] = []

    async def _offer(*_: Any, **__: Any) -> runner.Resumable:
        return runner.Resumable(run=failed, next_node="persist")

    async def _record(resume: str = "", **_: object) -> str:
        resumed.append(resume)
        return resume

    monkeypatch.setattr(runs_route, "resumable_run", _offer)
    monkeypatch.setattr(runner, "run_digest", _record)

    with TestClient(create_app(settings)) as client:
        stale = client.post("/runs/resume?lang=tr&run=someoldrun").text
        assert "artık devam ettirilemez" in stale
        assert resumed == []

        started = client.post(f"/runs/resume?lang=tr&run={failed.id}").text
        assert "Çalışıyor" in started
    assert resumed == [failed.id]
