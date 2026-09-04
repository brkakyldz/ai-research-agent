from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings
from ainews.db import Article, Source, Summary
from ainews.db.models import Run
from ainews.pipeline.nodes.dedupe import (
    dedupe_candidates,
    normalize_title,
    select_candidates,
)


async def _source(session: AsyncSession, name: str = "S", weight: float = 1.0) -> Source:
    src = Source(name=name, url=f"https://{name.lower()}.dev/feed", weight=weight)
    session.add(src)
    await session.flush()
    return src


async def _article(
    session: AsyncSession, src: Source, title: str, slug: str, *, age_hours: float = 1.0
) -> Article:
    art = Article(
        source_id=src.id,
        title=title,
        url=f"https://{src.name.lower()}.dev/{slug}",
        url_canonical=f"https://{src.name.lower()}.dev/{slug}",
        fetched_at=datetime.now(UTC) - timedelta(hours=age_hours),
        published_at=datetime.now(UTC) - timedelta(hours=age_hours),
    )
    session.add(art)
    await session.flush()
    return art


# -- title normalisation ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("OpenAI ships GPT-5.6 Luna | TechCrunch", "openai ships gpt 5 6 luna"),
        ("OpenAI ships GPT-5.6 Luna - The Verge", "openai ships gpt 5 6 luna"),
        ("Show HN: A tiny RAG library", "a tiny rag library"),
        ("  Spaced   out   title  ", "spaced out title"),
    ],
)
def test_normalisation_strips_outlet_and_section_noise(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


# -- fuzzy matching -----------------------------------------------------------


async def test_the_same_story_from_three_outlets_survives_once(
    session: AsyncSession, settings: Settings
) -> None:
    a = await _source(session, "A")
    b = await _source(session, "B")
    c = await _source(session, "C")
    await _article(session, a, "OpenAI releases GPT-5.6 Luna, its cheapest model yet", "1")
    await _article(session, b, "OpenAI ships GPT-5.6 Luna, cheapest model yet", "2")
    await _article(session, c, "GPT-5.6 Luna released by OpenAI as its cheapest model", "3")
    await session.commit()

    survivors, stats = await dedupe_candidates(session, settings)

    assert stats.n_candidates == 3
    assert len(survivors) == 1, "three write-ups of one story should cost one summary"
    assert stats.n_duplicates == 2

    marked = (await session.execute(select(Article).where(Article.dup_of.isnot(None)))).scalars()
    assert all(m.dup_of == survivors[0] for m in marked)


async def test_different_stories_are_not_merged(session: AsyncSession, settings: Settings) -> None:
    src = await _source(session, "A")
    await _article(session, src, "OpenAI releases GPT-5.6 Luna", "1")
    await _article(session, src, "Anthropic publishes interpretability research", "2")
    await _article(session, src, "Hugging Face raises a Series E", "3")
    await session.commit()

    survivors, stats = await dedupe_candidates(session, settings)
    assert len(survivors) == 3
    assert stats.n_duplicates == 0


async def test_duplicates_are_marked_not_deleted(session: AsyncSession, settings: Settings) -> None:
    """A wrong merge has to stay inspectable instead of becoming a missing row."""
    a = await _source(session, "A")
    b = await _source(session, "B")
    await _article(session, a, "Meta open-sources a new speech model", "1")
    await _article(session, b, "Meta open sources new speech model", "2")
    await session.commit()

    await dedupe_candidates(session, settings)
    assert len((await session.execute(select(Article))).scalars().all()) == 2


async def test_a_candidate_matching_an_already_summarised_article_is_dropped(
    session: AsyncSession, settings: Settings
) -> None:
    """Yesterday's story resurfacing in a slow feed must not be summarised twice."""
    a = await _source(session, "A")
    b = await _source(session, "B")
    old = await _article(session, a, "Google DeepMind announces Gemini 4", "1", age_hours=20)
    run = Run(kind="digest", language="tr")
    session.add(run)
    await session.flush()
    session.add(
        Summary(
            article_id=old.id,
            run_id=run.id,
            language="tr",
            title_local="x",
            summary="y",
            why_it_matters="z",
        )
    )
    await _article(session, b, "Gemini 4 announced by Google DeepMind", "2")
    await session.commit()

    survivors, stats = await dedupe_candidates(session, settings)
    assert survivors == []
    assert stats.n_duplicates == 1


async def test_articles_outside_the_lookback_window_do_not_suppress_new_ones(
    session: AsyncSession, settings: Settings
) -> None:
    """A story genuinely re-reported a week later is news again, not a duplicate."""
    a = await _source(session, "A")
    b = await _source(session, "B")
    stale = await _article(
        session,
        a,
        "EU AI Act enforcement begins",
        "1",
        age_hours=settings.dedupe_lookback_hours + 24,
    )
    run = Run(kind="digest", language="tr")
    session.add(run)
    await session.flush()
    session.add(
        Summary(
            article_id=stale.id,
            run_id=run.id,
            language="tr",
            title_local="x",
            summary="y",
            why_it_matters="z",
        )
    )
    await _article(session, b, "EU AI Act enforcement begins", "2")
    await session.commit()

    survivors, _ = await dedupe_candidates(session, settings)
    assert len(survivors) == 1


# -- candidate selection ------------------------------------------------------


async def test_already_summarised_articles_are_never_candidates_again(
    session: AsyncSession, settings: Settings
) -> None:
    """This is what makes "Run now" a delta: pressing it twice costs nothing twice."""
    src = await _source(session, "A")
    art = await _article(session, src, "A one-off story", "1")
    await session.commit()

    assert [a.id for a in await select_candidates(session, settings)] == [art.id]

    run = Run(kind="manual", language="tr")
    session.add(run)
    await session.flush()
    session.add(
        Summary(
            article_id=art.id,
            run_id=run.id,
            language="tr",
            title_local="x",
            summary="y",
            why_it_matters="z",
        )
    )
    await session.commit()

    assert await select_candidates(session, settings) == []


async def test_empty_input_is_not_an_error(session: AsyncSession, settings: Settings) -> None:
    survivors, stats = await dedupe_candidates(session, settings)
    assert survivors == []
    assert stats.n_candidates == 0
