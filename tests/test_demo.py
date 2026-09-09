"""The recorded day: `ainews demo export` and `ainews demo seed` (PLAN-V2 6.1).

The whole application is behind a paid run, so a reviewer who clones this reads
a README and an empty page with a button they cannot press. These tests are
about the file that fixes that: what it carries, what it deliberately does not,
and that a load produces a page rather than a set of rows.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import publish

from ainews.clock import local_day
from ainews.config import get_settings
from ainews.db import Article, Bulletin, BulletinItem, Run, Source, Summary, Verdict
from ainews.demo import DEMO_PATH, export_demo, load_demo, seed_demo, write_demo
from ainews.demo.seed import DemoEmpty, DemoNotEmpty


@pytest.fixture
async def recorded(session: AsyncSession) -> Bulletin:
    """One published day with a run behind it and a label on it."""
    src = Source(name="OpenAI", url="https://openai.com/news/rss.xml", weight=2.0)
    session.add(src)
    await session.flush()
    run = Run(
        kind="digest",
        language="tr",
        status="ok",
        n_summarized=3,
        est_cost_usd=0.02,
        model_summarize="gpt-5.6-luna",
    )
    session.add(run)
    await session.flush()

    ids: list[int] = []
    for index in range(3):
        art = Article(
            source_id=src.id,
            title=f"Haber {index}",
            url=f"https://openai.com/news/{index}",
            url_canonical=f"https://openai.com/news/{index}",
            body_text="Bir gövde metni. " * 40,
            body_source="feed",
        )
        session.add(art)
        await session.flush()
        summary = Summary(
            article_id=art.id,
            language="tr",
            title_local=f"Haber {index}",
            summary="Bir. Iki. Uc.",
            why_it_matters="Onemli.",
            tags_json='["openai"]',
            importance=4,
            key_fact="GPT-6 Astra",
        )
        session.add(summary)
        await session.flush()
        ids.append(summary.id)
    session.add(Verdict(summary_id=ids[0], verdict="wrong", reason="not_news", note="alakasiz"))
    await session.commit()
    return await publish(session, ids[:2], run=run, editor_note="Gunun ozeti.")


async def test_the_export_carries_no_article_bodies(
    session: AsyncSession, recorded: Bulletin
) -> None:
    """The line the fixtures already hold (ADR 0019 §3), on a second file.

    The repository is public and an article is someone else's text. Nothing on
    any page draws a body - a story links out to it - so the demo loses nothing
    a reviewer would see, and `ainews eval judge` says it has nothing to read.
    """
    data = await export_demo(session)
    blob = json.dumps(
        {
            "articles": data.articles,
            "summaries": data.summaries,
        },
        ensure_ascii=False,
    )
    assert "Bir gövde metni" not in blob
    assert all("body_text" not in article for article in data.articles)
    assert all("extra_text" not in article for article in data.articles)


async def test_the_export_carries_every_screen(session: AsyncSession, recorded: Bulletin) -> None:
    """The bulletin alone would leave four of the six pages empty, which is the
    state the demo exists to fix."""
    data = await export_demo(session)
    assert data.recorded_day == recorded.day
    assert len(data.bulletins) == 1 and len(data.bulletin_items) == 2
    assert len(data.summaries) == 3, "the third one is 'the other N' under the reading"
    assert len(data.runs) == 1, "so /runs and /runs/<id> have something to draw"
    assert len(data.verdicts) == 1, "and the reader's own label travels with it"


async def test_the_seed_moves_the_newest_bulletin_to_today(
    session: AsyncSession, recorded: Bulletin, tmp_path: object
) -> None:
    """Absolute dates would put the front page in the past on the second day
    after the export, and `/` shows *today's* bulletin - a demo whose front page
    is empty demonstrates nothing. The day it really ran is kept, and the band
    above the reading names it."""
    data = await export_demo(session)
    data.recorded_day = "2020-01-01"
    data.bulletins[0]["day_offset"] = 0

    # Emptied, so the seed is loading into a database with no archive in it -
    # which is the only state `seed_demo` accepts.
    await session.execute(BulletinItem.__table__.delete())
    await session.execute(Bulletin.__table__.delete())
    await session.execute(Verdict.__table__.delete())
    await session.execute(Summary.__table__.delete())
    await session.execute(Article.__table__.delete())
    await session.execute(Run.__table__.delete())
    await session.execute(Source.__table__.delete())
    await session.commit()
    # The rows just deleted are still in this session's identity map, and the
    # seed re-uses their primary keys.
    session.expunge_all()

    await seed_demo(session, data)

    bulletin = (await session.execute(select(Bulletin))).scalar_one()
    assert bulletin.day == local_day(), "the front page has something on it"
    assert data.recorded_day == "2020-01-01", "and the file still says when it really ran"
    assert (await session.execute(select(func.count(Summary.id)))).scalar_one() == 3
    assert (await session.execute(select(func.count(Verdict.id)))).scalar_one() == 1


async def test_seeding_over_a_real_archive_is_refused(
    session: AsyncSession, recorded: Bulletin
) -> None:
    """The one mistake here that cannot be undone from the interface: after it,
    the recording's summaries and a real run's are indistinguishable."""
    data = await export_demo(session)
    with pytest.raises(DemoNotEmpty, match="mix a recording"):
        await seed_demo(session, data)


async def test_exporting_an_empty_archive_says_what_to_do(session: AsyncSession) -> None:
    with pytest.raises(DemoEmpty, match="press the button once first"):
        await export_demo(session)


def test_the_shipped_demo_file_loads_and_holds_a_day() -> None:
    """The file that is committed, read as the application reads it."""
    assert DEMO_PATH.exists(), "the demo ships with the package"
    data = load_demo()
    assert data.recorded_day
    assert data.bulletins and data.summaries and data.runs
    assert all("body_text" not in article for article in data.articles)


async def test_a_seeded_database_draws_the_pages(client: TestClient, session: AsyncSession) -> None:
    """The exit condition: a stranger clones, starts it with no key, and reads.

    Every page off one seeded file, because a demo that fills the front page and
    leaves the run log empty is a screenshot rather than an application.
    """
    await seed_demo(session)

    front = client.get("/").text
    assert front.count('class="item ') > 0
    assert "/archive" in front
    assert client.get("/archive").text.count("b=") > 0
    assert "digest" in client.get("/runs?lang=en").text
    assert client.get("/sources").status_code == 200


async def test_the_page_says_it_is_a_recording(
    client: TestClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seeded news drawn as this morning's would be the one dishonest screen in
    the project. The band names the day the press really ran, on every page -
    a reviewer who lands on `/runs` sees a run log and has to be told those are
    a recording too."""
    from ainews.web import views

    await seed_demo(session)
    views.demo_notice.cache_clear()
    demo_settings = get_settings().model_copy(update={"demo_mode": True})
    monkeypatch.setattr(views, "get_settings", lambda: demo_settings)
    try:
        day = load_demo().recorded_day
        for path in ("/", "/runs", "/archive", "/sources"):
            body = client.get(path).text
            assert 'class="demo-band"' in body, path
            assert day in body, path
    finally:
        views.demo_notice.cache_clear()


def test_the_file_round_trips(tmp_path: object) -> None:
    data = load_demo()
    path = write_demo(data, DEMO_PATH.parent / "roundtrip.json")
    try:
        assert load_demo(path) == data
    finally:
        path.unlink()


def test_demo_mode_starts_no_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    """A demo polls no feeds.

    The one scheduled job left is the three-hourly collect (ADR 0015). On a
    reviewer's machine it would make network calls nobody asked for and file
    live articles beside a recording, after which the page is half recorded and
    half real and nothing on it says which half is which.
    """
    from ainews.web import app as app_module

    started: list[object] = []
    monkeypatch.setattr(app_module, "start_scheduler", lambda s: started.append(s))

    demo = get_settings().model_copy(update={"demo_mode": True, "scheduler_enabled": True})
    live = get_settings().model_copy(update={"demo_mode": False, "scheduler_enabled": True})

    with TestClient(app_module.create_app(demo)):
        pass
    assert started == [], "a demo polls nothing"

    with TestClient(app_module.create_app(live)):
        pass
    assert len(started) == 1, "and an ordinary install still winds the clock"
