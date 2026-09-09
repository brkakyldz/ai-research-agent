"""The clock, and it now winds only one job.

Collect - polling the feeds, no LLM and no cost - is on an interval, started in
the API process and stopped with it (ADR 0004). The digest is not: it spends
money, so a person presses the button (ADR 0015) and the `/runs` page tells them
when it is worth pressing. What is left here is the cheap half.

`coalesce=True` and `max_instances=1` together are what make a laptop safe as a
host: close the lid over four collect intervals and APScheduler would otherwise
fire four missed polls at once, all writing to a database with one writer.
Coalescing turns those four into one, and `max_instances` stops a slow poll from
being overlapped by the next one.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ainews.config import Settings, get_settings

log = logging.getLogger(__name__)

COLLECT_JOB_ID = "collect"

# How late a missed poll may still be worth doing. A collect that is four hours
# late is still useful; one from yesterday is not, the next cycle covers it.
MISFIRE_GRACE_SECONDS = 60 * 30


async def _collect_job() -> None:
    from ainews.pipeline.runner import RunBusy, run_collect

    try:
        await run_collect()
    except RunBusy:
        # A digest is running, and its first node is this same poll. Not a
        # failure and not worth a traceback: the next cycle is three hours out.
        log.info("scheduled collect skipped: a run is already in flight")
    except Exception:
        # The run row already records the failure; the scheduler must survive it,
        # because a scheduler that dies on one bad night stops every later night.
        log.exception("scheduled collect failed")


def build_scheduler(settings: Settings | None = None) -> AsyncIOScheduler:
    settings = settings or get_settings()
    scheduler = AsyncIOScheduler(
        timezone=settings.timezone,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": MISFIRE_GRACE_SECONDS,
        },
    )
    scheduler.add_job(
        _collect_job,
        IntervalTrigger(hours=settings.collect_interval_hours),
        id=COLLECT_JOB_ID,
        name="collect feeds",
        replace_existing=True,
        # An interval trigger's first fire is one whole interval away, so a
        # machine booted in the morning and shut down at night polls at most
        # twice a day and the first bulletin is written from whatever the last
        # session left behind. `next_run_time` overrides that first fire only;
        # every one after it is the interval again. The poll is free and the
        # feeds are conditional-GET, so an extra one at boot costs a handful of
        # 304s. Not `start_date`, which is read as "no earlier than" - set to
        # now it is already past, and the trigger skips to the next boundary,
        # which is the behaviour being fixed.
        next_run_time=datetime.now(UTC),
    )
    return scheduler


def start_scheduler(settings: Settings | None = None) -> AsyncIOScheduler:
    settings = settings or get_settings()
    scheduler = build_scheduler(settings)
    scheduler.start()
    log.info(
        "scheduler started (%s): collect every %dh; the digest is manual",
        settings.timezone,
        settings.collect_interval_hours,
    )
    return scheduler
