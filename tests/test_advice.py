"""The advice to run.

Nothing starts a digest but a person (ADR 0015), so the tool's whole remaining
opinion about timing is `build_advice`. Five things decide whether that opinion
is worth reading: that it counts from the last digest that actually produced a
bulletin, that a failure therefore does not push the suggestion a day out, that
it says "now" when there has never been one, that a run in flight and a missing
key are the only two states in which the button cannot be pressed, and that it
never reports either of those as a reason not to run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings, get_settings
from ainews.db import Run
from ainews.web.views import build_advice, format_gap


async def _run(
    session: AsyncSession,
    *,
    kind: str = "digest",
    status: str = "ok",
    hours_ago: float = 1.0,
) -> Run:
    started = datetime.now(UTC) - timedelta(hours=hours_ago)
    run = Run(
        kind=kind,
        language="tr",
        status=status,
        started_at=started,
        finished_at=started + timedelta(minutes=2),
    )
    session.add(run)
    await session.commit()
    return run


async def test_no_digest_ever_asks_to_be_run_now(session: AsyncSession) -> None:
    advice = await build_advice(session, "tr")
    assert advice.state == "never"
    assert advice.due_at is None and advice.gap is None
    assert advice.ready is True


async def test_a_recent_digest_counts_down_to_the_suggested_time(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIGEST_SUGGEST_AFTER_HOURS", "24")
    get_settings.cache_clear()
    await _run(session, hours_ago=2)

    advice = await build_advice(session, "tr")

    assert advice.state == "waiting"
    assert advice.seconds_left is not None
    # 24 hours from a digest two hours old, give or take the test's own runtime.
    assert 21.9 * 3600 < advice.seconds_left < 22.1 * 3600
    assert advice.gap == "21 sa 59 dk" or advice.gap.startswith("22 sa")
    assert advice.since is not None and advice.since.startswith(("1 sa", "2 sa"))


async def test_an_old_digest_is_due(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIGEST_SUGGEST_AFTER_HOURS", "24")
    get_settings.cache_clear()
    await _run(session, hours_ago=30)

    advice = await build_advice(session, "tr")

    assert advice.state == "due"
    assert advice.seconds_left is not None and advice.seconds_left < 0
    assert advice.ready is True


async def test_a_failed_run_does_not_reset_the_countdown(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mechanism this whole page exists for.

    A digest that ended in an error produced no bulletin. If the countdown were
    anchored on the last *finished* run rather than the last successful one, a
    run that failed at 07:02 would tell the reader to come back tomorrow - which
    is precisely the morning they need to press the button again.
    """
    monkeypatch.setenv("DIGEST_SUGGEST_AFTER_HOURS", "24")
    get_settings.cache_clear()
    await _run(session, hours_ago=30, status="ok")
    failure = await _run(session, hours_ago=1, status="error")

    advice = await build_advice(session, "tr")

    assert advice.state == "due", "a failure must not count as today's digest"
    # The failure is still the last thing that happened, and the page says so.
    assert advice.last is not None and advice.last.id == failure.id
    assert advice.last.status == "error"


async def test_a_run_in_flight_is_the_one_state_the_button_is_dead_in(
    session: AsyncSession,
) -> None:
    from ainews.pipeline import runner

    await _run(session, hours_ago=30)
    assert await runner.try_claim_digest("test")
    try:
        advice = await build_advice(session, "tr")
    finally:
        runner.release_digest("test")

    assert advice.state == "running"
    assert advice.ready is False


async def test_a_missing_key_blocks_the_press_and_says_why(
    session: AsyncSession, env_free_of_keys: None
) -> None:
    await _run(session, hours_ago=30)
    advice = await build_advice(session, "tr")

    assert advice.state == "blocked"
    assert advice.ready is False


async def test_the_collect_poll_is_reported_but_never_counted_as_a_digest(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Collect still runs on a clock, and it is not a bulletin.

    A three-hourly poll landing every three hours would otherwise keep resetting
    the countdown and the page would never once suggest a run.
    """
    monkeypatch.setenv("DIGEST_SUGGEST_AFTER_HOURS", "24")
    get_settings.cache_clear()
    await _run(session, kind="digest", hours_ago=30)
    await _run(session, kind="collect", hours_ago=0.5)

    advice = await build_advice(session, "tr")

    assert advice.state == "due"
    assert advice.last_collect_at is not None
    assert advice.last is not None and advice.last.kind == "digest"


@pytest.mark.parametrize(
    ("seconds", "tr", "en"),
    [
        (12, "birkaç saniye", "under a minute"),
        (9 * 60, "9 dk", "9m"),
        (3 * 3600, "3 sa", "3h"),
        (3 * 3600 + 25 * 60, "3 sa 25 dk", "3h 25m"),
        (-(3 * 3600), "3 sa", "3h"),
        (5 * 24 * 3600, "5 gün", "5d"),
    ],
)
def test_a_span_is_said_in_the_units_a_person_would_use(
    seconds: float, tr: str, en: str, settings: Settings
) -> None:
    assert format_gap(seconds, "tr") == tr
    assert format_gap(seconds, "en") == en
