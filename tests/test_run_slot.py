"""One run at a time, whatever started it.

The slot used to be the caller's job. Its docstring said "only if every path
takes it" and `cli.py` did not take it: `ainews digest` in a terminal alongside
a press on `/runs` selected the same unsummarised candidates and paid the model
for them twice. The slot is taken by the entry points themselves now, so these
call `run_digest` and `run_collect` directly - there is no route and no argument
that can skip it.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ainews.config import Settings
from ainews.db import Run
from ainews.pipeline import runner

pytestmark = pytest.mark.asyncio


async def test_the_cli_refuses_while_a_pressed_run_is_in_flight(
    settings: Settings, engine: AsyncEngine
) -> None:
    """The hole finding 1 of the 2026-09-08 audit named, from the CLI's side.

    `/runs/start` reserves the slot before scheduling its task; the terminal
    command calls `run_digest` with nothing reserved. Before the fix the second
    of those started a full paid run over the first one's candidates.
    """
    token = "manual:tr:x:y"
    assert await runner.reserve_slot(token, "digest")
    try:
        with pytest.raises(runner.RunBusy):
            await runner.run_digest(language="tr")
    finally:
        runner.release_slot(token)


async def test_a_refused_digest_opens_no_run_row(
    settings: Settings, engine: AsyncEngine, session: AsyncSession
) -> None:
    """The refusal lands before the run row opens, so a blocked digest does not
    leave a `running` row that `/runs` cannot explain."""
    token = "collect:held"
    assert await runner.reserve_slot(token, "collect")
    try:
        with pytest.raises(runner.RunBusy):
            await runner.run_digest(language="tr")
    finally:
        runner.release_slot(token)
    assert (await session.execute(select(Run))).scalars().all() == []


async def test_the_poll_stands_aside_for_a_digest(settings: Settings, engine: AsyncEngine) -> None:
    """A digest's first node is the same feed poll, and `_insert_new_items`
    checks then inserts - so an overlapping scheduled poll is an IntegrityError
    that fails one of the two runs, not a harmless duplicate."""
    token = "digest:held"
    assert await runner.reserve_slot(token, "digest")
    try:
        with pytest.raises(runner.RunBusy):
            await runner.run_collect(settings)
    finally:
        runner.release_slot(token)


async def test_the_scheduled_poll_survives_a_busy_slot(
    settings: Settings, engine: AsyncEngine
) -> None:
    """`RunBusy` must not reach the scheduler as a failure: a job that raises
    every three hours is a log full of tracebacks for the normal case."""
    from ainews.scheduler import _collect_job

    token = "digest:held"
    assert await runner.reserve_slot(token, "digest")
    try:
        await _collect_job()  # must not raise
    finally:
        runner.release_slot(token)


async def test_the_slot_is_released_when_the_run_ends(
    settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _quick(run_id: str, initial: object, s: Settings) -> None:
        return None

    monkeypatch.setattr(runner, "_invoke", _quick)
    await runner.run_digest(language="tr", settings=settings)
    assert not runner.run_in_flight()


async def test_the_slot_is_released_when_the_run_fails(
    settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one that matters: a slot held by a crashed run blocks every later
    press until the process is restarted."""

    async def _boom(run_id: str, initial: object, s: Settings) -> None:
        raise RuntimeError("node failed")

    monkeypatch.setattr(runner, "_invoke", _boom)
    with pytest.raises(RuntimeError):
        await runner.run_digest(language="tr", settings=settings)
    assert not runner.run_in_flight()


async def test_two_terminal_digests_at_once_become_one(
    settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = 0

    async def _slow(run_id: str, initial: object, s: Settings) -> None:
        nonlocal started
        started += 1
        await asyncio.sleep(0.2)

    monkeypatch.setattr(runner, "_invoke", _slow)
    results = await asyncio.gather(
        runner.run_digest(language="tr", settings=settings),
        runner.run_digest(language="tr", settings=settings),
        return_exceptions=True,
    )
    assert started == 1
    assert sum(isinstance(r, runner.RunBusy) for r in results) == 1
