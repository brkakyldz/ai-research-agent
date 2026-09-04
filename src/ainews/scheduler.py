"""The clock.

Two jobs, started in the API process and stopped with it (ADR 0004). They call
the same `run_collect` / `run_digest` the CLI and the "Run now" button call, so
there is exactly one definition of what each job does.

`coalesce=True` and `max_instances=1` together are what make a laptop safe as a
host: close the lid over four collect intervals and APScheduler would otherwise
fire four missed runs at once, all writing to a database with one writer.
Coalescing turns those four into one, and `max_instances` stops a slow digest
from being overlapped by the next one.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ainews.config import Settings, get_settings

log = logging.getLogger(__name__)

COLLECT_JOB_ID = "collect"
DIGEST_JOB_ID = "digest"

# How late a missed run may still be worth doing. A collect that is four hours
# late is still useful; one from yesterday is not, the next cycle covers it.
MISFIRE_GRACE_SECONDS = 60 * 30


async def _collect_job() -> None:
    from ainews.pipeline.runner import run_collect

    try:
        await run_collect()
    except Exception:
        # The run row already records the failure; the scheduler must survive it,
        # because a scheduler that dies on one bad night stops every later night.
        log.exception("scheduled collect failed")


async def _digest_job() -> None:
    from ainews.pipeline.runner import release_digest, run_digest, try_claim_digest

    settings = get_settings()
    if not settings.llm_configured:
        log.warning("skipping the scheduled digest: OPENAI_API_KEY is not set")
        return
    # `max_instances=1` only stops this job overlapping itself. It knows nothing
    # about a "Run now" press at 06:59, which is still in flight at 07:00 and is
    # summarising exactly the articles this run would summarise again.
    if not await try_claim_digest("scheduled"):
        log.warning("skipping the scheduled digest: a run is already in flight")
        return
    try:
        await run_digest(mode="digest")
    except Exception:
        log.exception("scheduled digest failed")
    finally:
        release_digest("scheduled")


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
    )
    scheduler.add_job(
        _digest_job,
        CronTrigger(hour=settings.digest_cron_hour, minute=settings.digest_cron_minute),
        id=DIGEST_JOB_ID,
        name="daily digest",
        replace_existing=True,
    )
    return scheduler


def start_scheduler(settings: Settings | None = None) -> AsyncIOScheduler:
    settings = settings or get_settings()
    scheduler = build_scheduler(settings)
    scheduler.start()
    log.info(
        "scheduler started (%s): collect every %dh, digest at %02d:%02d",
        settings.timezone,
        settings.collect_interval_hours,
        settings.digest_cron_hour,
        settings.digest_cron_minute,
    )
    return scheduler
