"""Read queries for the dashboard.

Kept apart from the routes because all four pages want the same three shapes -
a run, its stories, the tag counts - and a query written twice is a query that
disagrees with itself later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Language, get_settings
from ainews.db import Article, Run, Source, Summary


@dataclass(slots=True)
class Story:
    source: str
    url: str
    title_local: str
    summary: str
    why_it_matters: str
    importance: int
    tags: list[str]
    age: str


async def latest_digest_run(session: AsyncSession, language: Language) -> Run | None:
    """The most recent finished digest in this language.

    Language matters here: switching the toggle should show the last Turkish
    digest, not the last run of any kind rendered with Turkish buttons around
    English text.
    """
    return (
        await session.execute(
            select(Run)
            .where(Run.kind != "collect")
            .where(Run.language == language)
            .where(Run.status.in_(("ok", "partial")))
            .where(Run.n_summarized > 0)
            .order_by(Run.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _to_story(summary: Summary, article: Article, source_name: str, age: str) -> Story:
    try:
        tags = json.loads(summary.tags_json or "[]")
    except json.JSONDecodeError:
        tags = []
    return Story(
        source=source_name,
        url=article.url,
        title_local=summary.title_local,
        summary=summary.summary,
        why_it_matters=summary.why_it_matters,
        importance=summary.importance,
        tags=tags,
        age=age,
    )


async def stories_for_run(
    session: AsyncSession,
    run: Run,
    *,
    ranked_only: bool = True,
    tag: str | None = None,
) -> list[Story]:
    """The run's stories, in reading order.

    Ranked items come first in the order the ranker chose; the rest follow by
    importance. That is the same ordering the page's typography expresses, so a
    reader scanning downward sees the ink fade monotonically.
    """
    from ainews.web.views import relative_age

    query = (
        select(Summary, Article, Source.name)
        .join(Article, Article.id == Summary.article_id)
        .join(Source, Source.id == Article.source_id)
        .where(Summary.run_id == run.id)
    )
    if ranked_only:
        query = query.where(Summary.rank.isnot(None))
    query = query.order_by(
        Summary.rank.asc().nullslast(),
        Summary.importance.desc(),
        Summary.id.asc(),
    )

    stories = []
    for summary, article, source_name in (await session.execute(query)).all():
        story = _to_story(
            summary, article, source_name, relative_age(article.published_at, run.language)
        )
        if tag and tag not in story.tags:
            continue
        stories.append(story)
    return stories


async def count_unranked(session: AsyncSession, run: Run) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(Summary)
            .where(Summary.run_id == run.id)
            .where(Summary.rank.is_(None))
        )
    ).scalar_one()


async def tag_counts(session: AsyncSession, run: Run, limit: int = 12) -> list[tuple[str, int]]:
    """Tags across the run, most common first.

    Counted in Python rather than in SQL because the tags live in a JSON column;
    at a hundred rows a day that is not worth a second table.
    """
    rows = (
        await session.execute(select(Summary.tags_json).where(Summary.run_id == run.id))
    ).scalars()
    counts: dict[str, int] = {}
    for raw in rows:
        try:
            for tag in json.loads(raw or "[]"):
                counts[tag] = counts.get(tag, 0) + 1
        except json.JSONDecodeError:
            continue
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


async def recent_runs(session: AsyncSession, limit: int = 12) -> list[Run]:
    return list(
        (await session.execute(select(Run).order_by(Run.started_at.desc()).limit(limit))).scalars()
    )


async def digest_runs(session: AsyncSession, language: Language, limit: int = 30) -> list[Run]:
    return list(
        (
            await session.execute(
                select(Run)
                .where(Run.kind != "collect")
                .where(Run.language == language)
                .where(Run.n_summarized > 0)
                .order_by(Run.started_at.desc())
                .limit(limit)
            )
        ).scalars()
    )


def _fts_query(raw: str) -> str:
    """Turn a typed phrase into an FTS5 MATCH expression.

    Every token is quoted and given a prefix `*`, which does two jobs: it stops
    FTS5 from interpreting `AND`, `-` or `"` as operators and erroring on a
    perfectly ordinary search, and it makes Turkish suffixes stop mattering -
    "model" then finds "modeli" and "modelleri", which is the whole difference
    between a search box that works in Turkish and one that does not.
    """
    tokens = [t for t in raw.replace('"', " ").split() if t]
    return " ".join(f'"{token}"*' for token in tokens)


async def search_stories(
    session: AsyncSession, raw_query: str, language: Language, limit: int = 60
) -> list[Story]:
    from ainews.web.views import relative_age

    expression = _fts_query(raw_query)
    if not expression:
        return []

    hits = (
        await session.execute(
            text(
                "SELECT rowid FROM summaries_fts WHERE summaries_fts MATCH :q "
                "ORDER BY rank LIMIT :limit"
            ),
            {"q": expression, "limit": limit},
        )
    ).scalars()
    ids = list(hits)
    if not ids:
        return []

    rows = (
        await session.execute(
            select(Summary, Article, Source.name)
            .join(Article, Article.id == Summary.article_id)
            .join(Source, Source.id == Article.source_id)
            .where(Summary.id.in_(ids))
            .where(Summary.language == language)
            .order_by(Summary.created_at.desc())
        )
    ).all()

    return [
        _to_story(summary, article, name, relative_age(article.published_at, language))
        for summary, article, name in rows
    ]


@dataclass(slots=True)
class SourceRow:
    source: Source
    n_articles: int


async def source_rows(session: AsyncSession) -> list[SourceRow]:
    counts = dict(
        (
            await session.execute(
                select(Article.source_id, func.count()).group_by(Article.source_id)
            )
        ).all()
    )
    sources = (
        await session.execute(select(Source).order_by(Source.enabled.desc(), Source.name))
    ).scalars()
    return [SourceRow(source=s, n_articles=counts.get(s.id, 0)) for s in sources]


def digest_top_n() -> int:
    return get_settings().digest_top_n


def as_dicts(stories: list[Story]) -> list[dict[str, Any]]:
    return [s.__dict__ for s in stories]
