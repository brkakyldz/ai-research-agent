"""Page tests.

Every page is rendered from real rows rather than from a fixture of HTML, so a
template that references a field the query stopped returning fails here rather
than at seven in the morning.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ainews.config import Settings
from ainews.db import Article, Run, Source, Summary
from ainews.web.app import create_app
from ainews.web.i18n import LANGUAGE_COOKIE

TITLES = [
    ("OpenAI ucuz bir model duyurdu", 5),
    ("Avrupa yapay zeka yasasi icin rehber yayimladi", 4),
    ("Bir robotik girisimi yatirim aldi", 3),
    ("Kucuk bir kutuphane surum cikardi", 2),
    ("Topluluk derlemesi paylasildi", 1),
]


@pytest.fixture
async def digest(session: AsyncSession) -> Run:
    """One finished Turkish digest: three ranked stories and two below the fold."""
    src = Source(name="OpenAI", url="https://openai.com/news/rss.xml", weight=2.0)
    session.add(src)
    await session.flush()

    run = Run(
        kind="digest",
        language="tr",
        status="ok",
        n_summarized=len(TITLES),
        n_new=len(TITLES),
        est_cost_usd=0.0421,
        tokens_in=1000,
        tokens_out=400,
        editor_note="Gunun ortak konusu fiyat degil olcum.",
        finished_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()

    for index, (title, importance) in enumerate(TITLES):
        art = Article(
            source_id=src.id,
            title=title,
            url=f"https://openai.com/news/{index}",
            url_canonical=f"https://openai.com/news/{index}",
            published_at=datetime.now(UTC) - timedelta(hours=index + 1),
        )
        session.add(art)
        await session.flush()
        session.add(
            Summary(
                article_id=art.id,
                run_id=run.id,
                language="tr",
                title_local=title,
                summary=f"Ozet metni {index}. Ikinci cumle. Ucuncu cumle.",
                why_it_matters="Bu yuzden onemli.",
                tags_json=json.dumps(["openai", "models"] if index < 2 else ["policy"]),
                importance=importance,
                rank=index + 1 if index < 3 else None,
            )
        )
    await session.commit()
    return run


@pytest.fixture
def client(settings: Settings, engine: AsyncEngine) -> TestClient:
    """No lifespan: the `engine` fixture already built the schema, and running it
    again would point the app at a second engine."""
    return TestClient(create_app(settings))


# -- the digest page ----------------------------------------------------------


def test_the_digest_shows_the_note_and_the_ranked_stories(client: TestClient, digest: Run) -> None:
    body = client.get("/").text
    assert "Gunun ortak konusu" in body
    assert body.count('class="item ') == 3, "only ranked stories are above the fold"
    assert "OpenAI ucuz bir model duyurdu" in body


def test_importance_drives_the_class_that_drives_the_type(client: TestClient, digest: Run) -> None:
    """DESIGN.md's central claim: the score is visible only through typography."""
    body = client.get("/").text
    assert 'class="item p5"' in body
    assert 'class="item p4"' in body
    # No badge, no rank number - the whole point of the removals.
    assert "importance" not in body.lower()


def test_the_expander_reveals_everything_summarised(client: TestClient, digest: Run) -> None:
    assert 'href="/?lang=tr&amp;all=1"' in client.get("/").text
    assert client.get("/?all=1").text.count('class="item ') == 5


def test_an_empty_database_explains_itself(client: TestClient) -> None:
    body = client.get("/").text
    assert "Henüz digest yok" in body
    assert 'class="item ' not in body


def test_the_tag_filter_narrows_the_list(client: TestClient, digest: Run) -> None:
    body = client.get("/?all=1&tag=policy").text
    assert body.count('class="item ') == 3
    assert "OpenAI ucuz bir model duyurdu" not in body


# -- language -----------------------------------------------------------------


def test_the_language_toggle_switches_and_is_remembered(client: TestClient, digest: Run) -> None:
    response = client.get("/?lang=en")
    assert response.cookies.get(LANGUAGE_COOKIE) == "en"
    assert "today's note" in response.text or "No digest yet" in response.text

    # The seeded digest is Turkish, so English has nothing to show - which is
    # correct: a digest is written in one language per run.
    assert "Gunun ortak konusu" not in response.text


def test_the_remembered_language_survives_a_plain_visit(client: TestClient, digest: Run) -> None:
    client.get("/?lang=en")
    assert "No digest yet" in client.get("/").text
    client.get("/?lang=tr")
    assert "Gunun ortak konusu" in client.get("/").text


def test_the_toggle_link_keeps_the_rest_of_the_query(client: TestClient, digest: Run) -> None:
    body = client.get("/?all=1&tag=policy").text
    assert "all=1" in body and "tag=policy" in body and "lang=en" in body


# -- search -------------------------------------------------------------------


def test_search_finds_a_summary(client: TestClient, digest: Run) -> None:
    assert "Ozet metni 0" in client.get("/search?q=Ozet").text


def test_search_matches_a_turkish_suffix(client: TestClient, digest: Run) -> None:
    """Prefix matching is what makes the box work in an agglutinative language."""
    assert client.get("/search?q=model").text.count('class="item ') >= 1


def test_search_operators_do_not_crash_the_page(client: TestClient, digest: Run) -> None:
    """FTS5 raises on a bare AND or a stray quote; the box must not."""
    for query in ("model AND", '"', "a - b", "NEAR(", "*"):
        assert client.get("/search", params={"q": query}).status_code == 200


def test_an_empty_search_is_not_an_error(client: TestClient, digest: Run) -> None:
    assert client.get("/search?q=").status_code == 200


# -- archive, runs, sources ---------------------------------------------------


def test_the_archive_lists_and_renders_a_past_run(client: TestClient, digest: Run) -> None:
    body = client.get("/archive").text
    assert "Gunun ortak konusu" in body
    assert body.count('class="item ') == 3


def test_the_runs_page_shows_cost_and_duration(client: TestClient, digest: Run) -> None:
    body = client.get("/runs").text
    assert "$0.042" in body
    assert "digest" in body


async def test_failed_runs_surface_their_error(client: TestClient, session: AsyncSession) -> None:
    session.add(
        Run(
            kind="collect",
            language="tr",
            status="error",
            error="Hacker News: ConnectTimeout",
            finished_at=datetime.now(UTC),
        )
    )
    await session.commit()
    body = client.get("/runs").text
    assert "ConnectTimeout" in body
    assert 'class="bad"' in body


async def test_sources_page_lists_feeds_and_toggles_one(
    client: TestClient, session: AsyncSession
) -> None:
    src = Source(name="OpenAI", url="https://openai.com/news/rss.xml", consecutive_failures=3)
    session.add(src)
    await session.commit()

    assert "OpenAI" in client.get("/sources").text

    response = client.post(
        "/sources/toggle", data={"source_id": src.id, "lang": "tr"}, follow_redirects=False
    )
    assert response.status_code == 303
    await session.refresh(src)
    assert src.enabled is False

    client.post("/sources/toggle", data={"source_id": src.id, "lang": "tr"})
    await session.refresh(src)
    assert src.enabled is True
    assert src.consecutive_failures == 0, "re-enabling has to clear the strikes"


# -- run now ------------------------------------------------------------------


def test_run_now_refuses_without_a_key(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from ainews.config import get_settings

    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    with TestClient(create_app(get_settings())) as fresh:
        body = fresh.post("/runs/start?lang=tr").text
    assert "OPENAI_API_KEY" in body


def test_run_now_starts_one_run_and_refuses_a_second(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two presses must not become two runs competing for the same candidates."""
    import asyncio

    from ainews.web.routes import runs as runs_route

    started = 0

    async def _slow(language: str) -> None:
        nonlocal started
        started += 1
        runs_route._running.add("manual:test")
        await asyncio.sleep(0.3)
        runs_route._running.discard("manual:test")

    monkeypatch.setattr(runs_route, "_execute", _slow)

    first = client.post("/runs/start?lang=tr")
    second = client.post("/runs/start?lang=tr")

    assert "çalışıyor" in first.text
    assert "zaten" in second.text
    assert started == 1


# -- static -------------------------------------------------------------------


def test_the_page_loads_no_third_party_assets(client: TestClient, digest: Run) -> None:
    """It is a local tool; it has to open with the network unplugged."""
    body = client.get("/").text
    assert "https://" not in body.split("</head>")[0]


def test_fonts_and_scripts_are_served_locally(client: TestClient) -> None:
    for path in ("/static/theme.css", "/static/fonts.css", "/static/htmx.min.js"):
        assert client.get(path).status_code == 200
