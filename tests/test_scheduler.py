"""Scheduler tests.

There is one job left. The digest stopped being a cron on 2026-09-06 (ADR 0015)
and is now started by a person, so what this file asserts is everything that
decides whether the *feed poll* keeps happening: that the trigger fires on the
configured interval, that a laptop asleep through four intervals produces one
catch-up poll and not four, and that a poll which raises does not kill the
scheduler. That the digest is no longer registered is asserted here too, because
a job quietly coming back is exactly the regression the decision forbids.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ainews.config import Settings, get_settings
from ainews.scheduler import (
    COLLECT_JOB_ID,
    MISFIRE_GRACE_SECONDS,
    _collect_job,
    build_scheduler,
)


def test_the_poll_is_the_only_job(settings: Settings) -> None:
    """Nothing on a clock may spend money (ADR 0015)."""
    scheduler = build_scheduler(settings)
    assert {job.id for job in scheduler.get_jobs()} == {COLLECT_JOB_ID}


def test_collect_fires_on_the_configured_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLLECT_INTERVAL_HOURS", "3")
    get_settings.cache_clear()

    job = build_scheduler(get_settings()).get_job(COLLECT_JOB_ID)
    now = datetime.now(UTC)
    first = job.trigger.get_next_fire_time(None, now)
    second = job.trigger.get_next_fire_time(first, first)

    assert second - first == timedelta(hours=3)


async def test_a_sleeping_laptop_produces_one_catch_up_not_four(settings: Settings) -> None:
    """`coalesce` is the whole reason this is safe on a machine that suspends.

    Started paused: APScheduler only applies the job defaults when a job leaves
    the pending list, so a scheduler that was never started reports nothing.
    """
    scheduler = build_scheduler(settings)
    scheduler.start(paused=True)
    try:
        jobs = scheduler.get_jobs()
        assert len(jobs) == 1
        for job in jobs:
            assert job.coalesce is True, "four missed intervals must collapse into one poll"
            assert job.max_instances == 1, "a slow poll must not be overlapped by the next"
            assert job.misfire_grace_time == MISFIRE_GRACE_SECONDS
    finally:
        scheduler.shutdown(wait=False)


async def test_a_failing_collect_does_not_escape_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduler that dies on one bad night stops every later night."""
    import ainews.pipeline.runner as runner

    async def _boom(*_: object, **__: object) -> str:
        raise RuntimeError("feed host is down")

    monkeypatch.setattr(runner, "run_collect", _boom)
    await _collect_job()  # must not raise
