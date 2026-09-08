"""Every timestamp comes back the way it went in: aware, in UTC.

`DateTime(timezone=True)` is a promise SQLite has no type to keep — it stores a
string, so every value read back was naive and three readers re-attached UTC by
hand. `db.models.UTCDateTime` moves the conversion to the boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ainews.db import Article, Run, RunStep, Source, Summary


async def test_a_run_reads_back_aware(session: AsyncSession, engine: AsyncEngine) -> None:
    run = Run(kind="digest", language="tr")
    session.add(run)
    await session.commit()
    session.expunge_all()

    loaded = (await session.execute(select(Run))).scalar_one()
    assert loaded.started_at.tzinfo is not None
    assert loaded.started_at.utcoffset() == timedelta(0)


async def test_an_offset_datetime_is_converted_not_truncated(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """An aware value in another zone is the same instant in UTC, not the same
    wall clock relabelled — which is what dropping the offset would have made
    it, three hours out on a machine in Antalya."""
    istanbul = timezone(timedelta(hours=3))
    written = datetime(2026, 9, 8, 18, 30, tzinfo=istanbul)
    run = Run(kind="digest", language="tr", started_at=written)
    session.add(run)
    await session.commit()
    session.expunge_all()

    loaded = (await session.execute(select(Run))).scalar_one()
    assert loaded.started_at == written
    assert loaded.started_at.hour == 15


async def test_subtracting_two_stored_timestamps_does_not_raise(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """`steps._fan_out_row` brackets the fan-out between two nodes' timestamps
    and falls back to `utcnow()` when the second is missing. Naive minus aware
    is a `TypeError`, and it fell on exactly the path that matters most: the one
    taken when every branch of the fan-out failed."""
    run = Run(kind="digest", language="tr")
    session.add(run)
    await session.flush()
    session.add(RunStep(run_id=run.id, node="enrich", finished_at=datetime.now(UTC)))
    await session.commit()
    session.expunge_all()

    step = (await session.execute(select(RunStep))).scalar_one()
    assert (datetime.now(UTC) - step.finished_at).total_seconds() >= 0
    assert (datetime.now(UTC) - step.started_at).total_seconds() >= 0


async def test_every_timestamped_table_reads_back_aware(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    source = Source(
        name="OpenAI", url="https://example.com/f.xml", last_fetched_at=datetime.now(UTC)
    )
    session.add(source)
    await session.flush()
    article = Article(
        source_id=source.id,
        url="https://example.com/a",
        url_canonical="https://example.com/a",
        title="t",
        published_at=datetime.now(UTC),
    )
    run = Run(kind="digest", language="tr")
    session.add_all([article, run])
    await session.flush()
    session.add(
        Summary(
            run_id=run.id,
            article_id=article.id,
            language="tr",
            title_local="t",
            summary="s",
            why_it_matters="w",
            importance=3,
        )
    )
    await session.commit()
    session.expunge_all()

    for value in (
        (await session.execute(select(Source))).scalar_one().last_fetched_at,
        (await session.execute(select(Article))).scalar_one().fetched_at,
        (await session.execute(select(Article))).scalar_one().published_at,
        (await session.execute(select(Summary))).scalar_one().created_at,
    ):
        assert value is not None and value.tzinfo is not None
