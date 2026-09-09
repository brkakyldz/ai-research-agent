"""What a page costs to draw.

Finding 7 of the 2026-09-08 audit. None of these are about speed at today's row
counts — the archive is three bulletins — they are about work that grows with an
archive the project has decided to keep, and about the same question being asked
twice in one render, which is how two numbers on one screen come to disagree.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.factories import publish

from ainews.db import Article, Bulletin, Run, Source, Summary


@contextmanager
def counted(engine: AsyncEngine, table: str) -> Iterator[list[str]]:
    """Every SELECT issued against one table while the block runs."""
    seen: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and f" {table}" in statement:
            seen.append(" ".join(statement.split()))

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)


@pytest.fixture
async def one_bulletin(session: AsyncSession) -> Bulletin:
    """Three summaries, two of them published. The third is what `?all=1` adds."""
    source = Source(name="OpenAI", url="https://openai.com/rss.xml", weight=2.0)
    session.add(source)
    await session.flush()
    run = Run(kind="digest", language="tr", status="ok", n_summarized=3)
    session.add(run)
    await session.flush()
    published: list[int] = []
    for n in range(3):
        article = Article(
            source_id=source.id,
            url=f"https://openai.com/{n}",
            url_canonical=f"https://openai.com/{n}",
            title=f"Story {n}",
        )
        session.add(article)
        await session.flush()
        summary = Summary(
            article_id=article.id,
            language="tr",
            title_local=f"Haber {n}",
            summary="s",
            why_it_matters="w",
            importance=4,
            tags_json='["openai", "agents"]' if n else '["openai"]',
        )
        session.add(summary)
        await session.flush()
        if n < 2:
            published.append(summary.id)
    await session.commit()
    return await publish(session, published, run=run)


async def test_the_shell_asks_for_the_latest_run_once(
    client: TestClient, engine: AsyncEngine, one_bulletin: Bulletin
) -> None:
    """The advice block is built once per render and every reader of it takes
    the same object - being two of them, they could disagree."""
    with counted(engine, "runs") as statements:
        assert client.get("/runs").status_code == 200
    latest = [s for s in statements if "ORDER BY runs.started_at DESC" in s and "LIMIT" in s]
    finished = [s for s in latest if "runs.status !=" in s]
    assert len(finished) == 1, finished


async def test_the_full_list_counts_its_tags_once(
    client: TestClient, engine: AsyncEngine, one_bulletin: Bulletin
) -> None:
    """`?all=1` needs the tags of the whole day and of the published stories,
    and read every `tags_json` in the day twice to get them."""
    with counted(engine, "summaries") as statements:
        assert client.get("/?all=1").status_code == 200
    tag_scans = [s for s in statements if s.startswith("SELECT summaries.id, summaries.tags_json")]
    assert len(tag_scans) == 1, tag_scans


async def test_a_tag_filter_narrows_in_sql(
    client: TestClient, engine: AsyncEngine, one_bulletin: Bulletin
) -> None:
    """The exact membership test still runs in Python - a malformed value must
    be skipped, not fail the query - but the rows it runs over are narrowed."""
    with counted(engine, "summaries") as statements:
        body = client.get("/?tag=agents").text
    assert "Haber 1" in body
    assert any("LIKE" in s for s in statements), statements


async def test_the_filter_still_decides_membership_exactly(
    client: TestClient, one_bulletin: Bulletin
) -> None:
    """A tag that is a substring of another must not match it. `LIKE` narrows;
    the list is still what the Python check kept. Asserted on the headlines the
    story list drew, not on the page - the shell's own search box says "Haber"
    on every render."""
    assert "Haber 1" not in client.get("/?tag=agent").text
    assert "Haber 2" not in client.get("/?tag=agent").text
    assert "Haber 1" in client.get("/?tag=agents").text


async def test_the_ranker_names_its_sources_in_one_query(
    session: AsyncSession, engine: AsyncEngine, one_bulletin: Bulletin
) -> None:
    """Two `session.get` calls per summary is a hundred and eighty round trips
    on a ninety-story day, to read one column of one table. The pool query
    selects the source name and weight beside the summary, so there is no
    second pass at all."""
    from ainews.pipeline.nodes.rank import day_pool

    with counted(engine, "articles") as statements:
        pool = await day_pool(session, one_bulletin.day, "tr")
    assert len(statements) == 1, statements
    assert len(pool) == 3
    assert all(c.source == "OpenAI" and c.weight == 2.0 for c in pool)
