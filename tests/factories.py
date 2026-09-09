"""Building a published bulletin, for tests that are about something else.

Nine test files want "a page with these stories on it" and none of them is
about how a bulletin is assembled - `test_ranking.py` is. Written once here so
the shape of a published day lives in one place and a schema change is one edit.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.clock import local_day
from ainews.db import Bulletin, BulletinItem, Run

# The default tier for each place in the order, which is the shape the page
# draws: one lead, a few majors, the rest. A test that cares about the tier
# passes its own.
DEFAULT_TIERS = ("lead", "major", "major", "major", "notable")


def tier_for(position: int) -> str:
    return DEFAULT_TIERS[position - 1] if position <= len(DEFAULT_TIERS) else "brief"


async def publish(
    session: AsyncSession,
    summary_ids: Sequence[int],
    *,
    day: str | None = None,
    language: str = "tr",
    version: int | None = None,
    editor_note: str | None = "Gunun ortak konusu fiyat degil olcum.",
    agreement: float | None = 0.82,
    run: Run | None = None,
    tiers: Sequence[str] | None = None,
) -> Bulletin:
    """One bulletin over `summary_ids`, in the order given.

    `version` counts up within a day by default, the way `persist` does, so a
    test that publishes twice gets two versions of one day rather than an
    integrity error about a key it was not thinking about.
    """
    day = day or local_day()
    if version is None:
        version = (
            await session.execute(
                select(func.coalesce(func.max(Bulletin.version), 0) + 1)
                .where(Bulletin.day == day)
                .where(Bulletin.language == language)
            )
        ).scalar_one()
    bulletin = Bulletin(
        day=day,
        language=language,
        version=version,
        editor_note=editor_note,
        model_rank="gpt-5.6-luna",
        agreement=agreement,
        est_cost_usd=0.004,
        run_id=run.id if run else None,
    )
    session.add(bulletin)
    await session.flush()
    for position, summary_id in enumerate(summary_ids, start=1):
        session.add(
            BulletinItem(
                bulletin_id=bulletin.id,
                summary_id=summary_id,
                position=position,
                tier=tiers[position - 1] if tiers else tier_for(position),
            )
        )
    await session.commit()
    return bulletin
