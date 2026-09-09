"""A capped list says so, and offers the rest.

The archive stopped at 30, search at 60, the verdicts at 200 and the run log at
40, and none of them said anything about it. A page showing thirty of
forty-seven with no line saying so is not a page with a limit on it, it is a
page that is wrong: the reader cannot tell a short list from a truncated one, in
an app whose whole argument is that the archive is kept.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.factories import publish

from ainews.db import Article, Bulletin, Run, Source, Summary, Verdict
from ainews.web.queries import LIMIT_CEILING, Page, page_limit


@pytest.fixture
async def many_bulletins(session: AsyncSession) -> list[Bulletin]:
    source = Source(name="OpenAI", url="https://openai.com/rss.xml")
    session.add(source)
    await session.flush()
    runs = []
    for n in range(35):
        run = Run(kind="digest", language="tr", status="ok", n_summarized=15)
        session.add(run)
        await session.flush()
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
            title_local=f"Bulten {n}",
            summary="s",
            why_it_matters="w",
            importance=4,
        )
        session.add(summary)
        await session.flush()
        await session.commit()
        # One bulletin per day, thirty-five days back, so the archive has
        # thirty-five rows rather than thirty-five versions of today.
        runs.append(
            await publish(
                session,
                [summary.id],
                day=(date.today() - timedelta(days=n)).isoformat(),
                run=run,
            )
        )
    return runs


def test_a_page_knows_when_it_is_truncated() -> None:
    assert Page(rows=[1, 2, 3], total=3, limit=30).more is False
    assert Page(rows=[1, 2, 3], total=47, limit=3).more is True


def test_the_next_window_always_grows() -> None:
    """A doubling that could not grow would be a link that does nothing - the
    case is a limit of zero, or one clamped below the rows already drawn."""
    assert Page(rows=[1] * 30, total=47, limit=30).next_limit == 60
    assert Page(rows=[1], total=9, limit=0).next_limit == 2


@pytest.mark.parametrize(
    "raw,expected", [(None, 30), (0, 30), (-5, 30), (10, 10), (LIMIT_CEILING + 1, LIMIT_CEILING)]
)
def test_the_limit_from_the_url_is_clamped(raw: int | None, expected: int) -> None:
    """A typed or pasted `?limit=1000000` must not turn a read into a scan the
    page then tries to render."""
    assert page_limit(raw, 30) == expected


async def test_the_archive_says_how_many_bulletins_it_is_not_showing(
    client: TestClient, engine: AsyncEngine, many_bulletins: list[Bulletin]
) -> None:
    body = client.get("/archive").text
    assert "30/35 gösteriliyor" in body
    assert "limit=60" in body, "and a way to the rest"


async def test_raising_the_limit_shows_the_rest(
    client: TestClient, engine: AsyncEngine, many_bulletins: list[Bulletin]
) -> None:
    body = client.get("/archive?limit=60").text
    assert "Daha fazla" not in body, "nothing is left behind, so nothing is offered"


async def test_a_bulletin_outside_the_window_still_opens_by_id(
    client: TestClient, engine: AsyncEngine, many_bulletins: list[Bulletin]
) -> None:
    """A link into the archive must not depend on how far the list was opened.
    The oldest bulletin is past the default window of thirty."""
    oldest = many_bulletins[-1]
    body = client.get(f"/archive?b={oldest.id}").text
    assert f"b={oldest.id}" in body


async def test_the_run_log_says_how_much_of_itself_it_draws(
    client: TestClient, engine: AsyncEngine, many_bulletins: list[Bulletin]
) -> None:
    assert "Daha fazla" not in client.get("/runs").text, "35 runs fit in the window of 40"
    assert "Daha fazla" in client.get("/runs?limit=10").text


async def test_search_counts_the_hits_it_did_not_draw(
    client: TestClient, engine: AsyncEngine, many_bulletins: list[Bulletin]
) -> None:
    """FTS5 answers the count without materialising the rows, which is the whole
    reason a search box can afford to say "60 of 214"."""
    body = client.get("/search?q=Bulten&limit=5").text
    assert "5/35" in body
    assert "q=Bulten" in body and "limit=10" in body, "the query survives the link"


async def test_the_labels_page_offers_the_rest(
    client: TestClient, engine: AsyncEngine, session: AsyncSession, many_bulletins: list[Bulletin]
) -> None:
    from sqlalchemy import select

    ids = list((await session.execute(select(Summary.id))).scalars())
    for summary_id in ids[:5]:
        session.add(Verdict(summary_id=summary_id, verdict="ok"))
    await session.commit()

    assert "Daha fazla" not in client.get("/runs/verdicts").text
    body = client.get("/runs/verdicts?limit=2").text
    assert "2/5" in body
    assert "Daha fazla" in body
