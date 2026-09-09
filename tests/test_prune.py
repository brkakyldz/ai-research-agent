"""`ainews prune`, and the line it will not cross.

The archive is the point of the tool, so nothing here removes a summary, a
bulletin, a run row, a verdict or a judge finding. What it removes is the one
thing no reader and no command can reach: articles past the collect horizon that
were never summarised.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ainews.config import Settings
from ainews.db import Article, Run, Source, Summary
from ainews.db.models import utcnow
from ainews.pipeline import runner
from ainews.pipeline.prune import prune


async def test_an_article_past_the_horizon_with_no_summary_goes(
    settings: Settings, engine: AsyncEngine, session: AsyncSession
) -> None:
    """`nodes/dedupe._unsummarized` only looks inside `collect_max_age_days`, so
    this row is not a candidate now and cannot become one later."""
    source = Source(name="OpenAI", url="https://openai.com/rss.xml")
    session.add(source)
    await session.flush()
    stale = Article(
        source_id=source.id,
        url="https://openai.com/old",
        url_canonical="https://openai.com/old",
        title="Old",
        fetched_at=utcnow() - timedelta(days=settings.collect_max_age_days + 1),
    )
    fresh = Article(
        source_id=source.id,
        url="https://openai.com/new",
        url_canonical="https://openai.com/new",
        title="New",
    )
    session.add_all([stale, fresh])
    await session.commit()

    report = await prune(settings)
    assert report.articles == 1
    kept = list((await session.execute(select(Article.title))).scalars())
    assert kept == ["New"]


async def test_a_summarised_article_is_never_pruned(
    settings: Settings, engine: AsyncEngine, session: AsyncSession
) -> None:
    """However old it is. It is the story behind an archived bulletin, and the
    story list joins to it for the source name and the link."""
    source = Source(name="OpenAI", url="https://openai.com/rss.xml")
    run = Run(kind="digest", language="tr", status="ok", n_summarized=1)
    session.add_all([source, run])
    await session.flush()
    article = Article(
        source_id=source.id,
        url="https://openai.com/ancient",
        url_canonical="https://openai.com/ancient",
        title="Ancient",
        fetched_at=utcnow() - timedelta(days=400),
    )
    session.add(article)
    await session.flush()
    session.add(
        Summary(
            article_id=article.id,
            language="tr",
            title_local="Eski",
            summary="s",
            why_it_matters="w",
            importance=4,
        )
    )
    await session.commit()

    assert (await prune(settings)).articles == 0
    assert (await session.execute(select(Article))).scalars().all() != []
    assert (await session.execute(select(Summary))).scalars().all() != []


async def test_a_duplicate_of_a_pruned_article_survives_with_its_mark_cleared(
    settings: Settings, engine: AsyncEngine, session: AsyncSession
) -> None:
    """`dup_of` is inspectable history, not a reason to keep a dead row alive -
    and `PRAGMA foreign_keys=ON` would refuse the delete while it points."""
    source = Source(name="OpenAI", url="https://openai.com/rss.xml")
    run = Run(kind="digest", language="tr", status="ok", n_summarized=1)
    session.add_all([source, run])
    await session.flush()
    original = Article(
        source_id=source.id,
        url="https://openai.com/a",
        url_canonical="https://openai.com/a",
        title="A",
        fetched_at=utcnow() - timedelta(days=90),
    )
    session.add(original)
    await session.flush()
    rewrite = Article(
        source_id=source.id,
        url="https://openai.com/b",
        url_canonical="https://openai.com/b",
        title="B",
        fetched_at=utcnow() - timedelta(days=90),
        dup_of=original.id,
    )
    session.add(rewrite)
    await session.flush()
    session.add(
        Summary(
            article_id=rewrite.id,
            language="tr",
            title_local="B",
            summary="s",
            why_it_matters="w",
            importance=3,
        )
    )
    await session.commit()

    assert (await prune(settings)).articles == 1
    session.expire_all()  # the delete happened in the runner's own session
    survivors = list((await session.execute(select(Article))).scalars())
    assert [a.title for a in survivors] == ["B"]
    assert survivors[0].dup_of is None


async def test_pruning_refuses_while_a_run_is_in_flight(
    settings: Settings, engine: AsyncEngine
) -> None:
    """A digest in flight is collecting the very articles this counts, and an
    article summarised a second after the count is one this would then delete
    out from under a live summary."""
    assert await runner.reserve_slot("digest:held", "digest")
    try:
        with pytest.raises(runner.RunBusy):
            await prune(settings)
    finally:
        runner.release_slot("digest:held")


async def test_the_command_reports_and_deletes_nothing_on_a_dry_run(
    settings: Settings, engine: AsyncEngine, session: AsyncSession, capsys: pytest.CaptureFixture
) -> None:
    from ainews.cli import _prune

    source = Source(name="OpenAI", url="https://openai.com/rss.xml")
    session.add(source)
    await session.flush()
    session.add(
        Article(
            source_id=source.id,
            url="https://openai.com/old",
            url_canonical="https://openai.com/old",
            title="Old",
            fetched_at=utcnow() - timedelta(days=settings.collect_max_age_days + 1),
        )
    )
    await session.commit()

    assert await _prune(dry_run=True) == 0
    said = capsys.readouterr().out
    assert "would drop 1 unsummarised article(s)" in said
    assert "never pruned" in said
    assert (await session.execute(select(Article))).scalars().all() != []
