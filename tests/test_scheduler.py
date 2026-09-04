"""Scheduler tests.

M5's acceptance criterion is "three unattended 07:00 digests on consecutive
days". Waiting three days is not a test, so what is asserted instead is
everything that decides whether those three runs happen: that the triggers fire
at the right times, that a laptop asleep through four intervals produces one
catch-up run and not four, that a job which raises does not kill the scheduler,
and that a missing key skips the digest instead of failing it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ainews.config import Settings, get_settings
from ainews.scheduler import (
    COLLECT_JOB_ID,
    DIGEST_JOB_ID,
    MISFIRE_GRACE_SECONDS,
    _collect_job,
    _digest_job,
    build_scheduler,
)


def test_both_jobs_are_registered(settings: Settings) -> None:
    scheduler = build_scheduler(settings)
    ids = {job.id for job in scheduler.get_jobs()}
    assert ids == {COLLECT_JOB_ID, DIGEST_JOB_ID}


def test_the_digest_fires_at_the_configured_local_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIGEST_CRON_HOUR", "7")
    monkeypatch.setenv("DIGEST_CRON_MINUTE", "0")
    monkeypatch.setenv("TIMEZONE", "Europe/Istanbul")
    get_settings.cache_clear()

    scheduler = build_scheduler(get_settings())
    job = scheduler.get_job(DIGEST_JOB_ID)
    zone = ZoneInfo("Europe/Istanbul")
    after = datetime(2026, 9, 4, 12, 0, tzinfo=zone)

    fires = []
    previous = None
    for _ in range(3):
        previous = job.trigger.get_next_fire_time(previous, previous or after)
        fires.append(previous)

    assert [f.hour for f in fires] == [7, 7, 7]
    assert [f.minute for f in fires] == [0, 0, 0]
    # Three consecutive days, which is what M5 asks for.
    assert (fires[1].date() - fires[0].date()) == timedelta(days=1)
    assert (fires[2].date() - fires[1].date()) == timedelta(days=1)


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
        assert len(jobs) == 2
        for job in jobs:
            assert job.coalesce is True, "four missed intervals must collapse into one run"
            assert job.max_instances == 1, "a slow digest must not be overlapped by the next"
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


async def test_the_digest_is_skipped_rather_than_failed_without_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ainews.pipeline.runner as runner

    called = False

    async def _should_not_run(*_: object, **__: object) -> str:
        nonlocal called
        called = True
        return "x"

    monkeypatch.setattr(runner, "run_digest", _should_not_run)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()

    await _digest_job()
    assert called is False


async def test_the_digest_job_calls_the_same_runner_the_button_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ainews.pipeline.runner as runner

    seen: dict[str, object] = {}

    async def _record(*args: object, **kwargs: object) -> str:
        seen.update(kwargs)
        return "run-id"

    monkeypatch.setattr(runner, "run_digest", _record)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    get_settings.cache_clear()

    await _digest_job()
    assert seen == {"mode": "digest"}
