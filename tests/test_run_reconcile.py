"""Run rows a killed process left open, and what `partial` is allowed to mean.

Two separate repairs to the same thing: the status column on `runs`, which is
what `/runs` reads to tell the reader what state the machine is in. One says a
run is going when nothing is; the other says a bulletin is degraded when it is
whole.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.db import Run
from ainews.db.models import utcnow
from ainews.pipeline import graph as graph_module
from ainews.pipeline.nodes.collect import CollectStats
from ainews.pipeline.runner import reconcile_orphaned_runs


async def test_a_run_left_running_is_closed_at_startup(session: AsyncSession) -> None:
    """`_fail_run` closes a run that *raised*. A killed process does not raise:
    a closed laptop, `docker compose down`, Ctrl-C. Without this the row keeps
    `status='running'` and `finished_at` NULL forever."""
    orphan = Run(kind="collect", language="tr", status="running")
    session.add(orphan)
    await session.commit()

    closed = await reconcile_orphaned_runs()

    assert closed == 1
    await session.refresh(orphan)
    assert orphan.status == "error"
    assert orphan.finished_at is not None
    assert orphan.error == "process ended before the run did"


async def test_a_killed_digest_becomes_resumable(session: AsyncSession) -> None:
    """Resume asks for `status='error'`, and a killed run never reached one, so
    the run the reader most wants to finish was the one never offered."""
    orphan = Run(kind="digest", language="tr", status="running")
    session.add(orphan)
    await session.commit()

    await reconcile_orphaned_runs()

    await session.refresh(orphan)
    assert orphan.status == "error"


async def test_finished_runs_are_left_alone(session: AsyncSession) -> None:
    finished = Run(kind="digest", language="tr", status="ok", finished_at=utcnow())
    partial = Run(kind="digest", language="tr", status="partial", finished_at=utcnow())
    session.add_all([finished, partial])
    await session.commit()

    assert await reconcile_orphaned_runs() == 0

    await session.refresh(finished)
    await session.refresh(partial)
    assert finished.status == "ok"
    assert partial.status == "partial"


async def test_reconciling_twice_changes_nothing_the_second_time(
    session: AsyncSession,
) -> None:
    session.add(Run(kind="collect", language="tr", status="running"))
    await session.commit()

    assert await reconcile_orphaned_runs() == 1
    assert await reconcile_orphaned_runs() == 0


async def test_a_dead_feed_does_not_make_the_bulletin_partial(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One rotting feed used to mark every digest `partial` for five polls.

    `collect` copied its feed errors into the run's error list and `persist_run`
    reads that list to decide the whole run's status, so the word stopped
    telling the reader anything. Which feeds answered is a property of the poll
    and it stays on the collect step row.
    """

    async def _failing_collect(*_: object, **__: object) -> CollectStats:
        return CollectStats(n_seen=3, n_new=1, n_sources=2, errors=["Anthropic mirror: HTTP 404"])

    async def _no_enrich(*_: object, **__: object) -> object:
        from ainews.pipeline.nodes.enrich import EnrichStats

        return EnrichStats()

    monkeypatch.setattr(graph_module, "collect_articles", _failing_collect)
    monkeypatch.setattr(graph_module, "enrich_articles", _no_enrich)

    run = Run(kind="digest", language="en")
    session.add(run)
    await session.commit()

    state = await graph_module.collect_node({"run_id": run.id})

    # The counts still travel; the feed's failure does not. `persist_run` reads
    # `errors` and nothing else to decide between `ok` and `partial`, so an
    # empty list here is the whole fix.
    assert state["n_collected"] == 3
    assert state["n_new"] == 1
    assert "errors" not in state


async def test_the_collect_steps_row_still_carries_the_feed_error(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error is not dropped, it is filed where the run detail page reads it."""
    from ainews.db import RunStep

    async def _failing_collect(*_: object, **__: object) -> CollectStats:
        return CollectStats(n_seen=3, n_new=1, n_sources=2, errors=["Anthropic mirror: HTTP 404"])

    monkeypatch.setattr(graph_module, "collect_articles", _failing_collect)

    run = Run(kind="digest", language="en")
    session.add(run)
    await session.commit()

    await graph_module.collect_node({"run_id": run.id})

    step = (
        await session.execute(
            select(RunStep).where(RunStep.run_id == run.id, RunStep.node == "collect")
        )
    ).scalar_one()
    assert step.status == "partial"
    assert "Anthropic mirror" in (step.detail or "")
