"""The pages, rendered from real rows.

Every page is drawn from a seeded run rather than from a fixture of HTML, so a
template referencing a field the query stopped returning fails here rather than
at seven in the morning.

Three files, not one. This was 1,307 lines and 83 tests, which is a file nobody
opens to read - the shell's own tests are `test_web_shell.py` and the press is
`test_run_button.py`, and each of the three is now about one thing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import get_settings
from ainews.db import Article, Run, Source, Summary
from ainews.web.i18n import LANGUAGE_COOKIE

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


async def test_the_feed_is_ordered_by_importance_not_by_rank(
    client: TestClient, session: AsyncSession
) -> None:
    """The ink has to fade downwards, and until 2026-09-08 it did not.

    `rank` led the sort and `importance` only broke its ties. They are two
    different measurements - the ranker's order over the whole run against the
    model's 1-5 score on one story - so they disagree constantly, and a real day
    came out p4, p3, p3, p2, p3. On a page whose only ranking indicator is the
    size of the headline (ADR 0014) that reads as no order at all. `rank` still
    chooses which stories are on the page, which is the job it was written for.

    Seeded so the two orders are exact opposites: the least important story is
    ranked first.
    """
    src = Source(name="OpenAI", url="https://openai.com/news/rss.xml")
    session.add(src)
    await session.flush()
    run = Run(
        kind="digest", language="tr", status="ok", n_summarized=4, finished_at=datetime.now(UTC)
    )
    session.add(run)
    await session.flush()

    for position, importance in enumerate([2, 5, 3, 4]):
        art = Article(
            source_id=src.id,
            title=f"Haber {importance}",
            url=f"https://openai.com/news/o{position}",
            url_canonical=f"https://openai.com/news/o{position}",
            published_at=datetime.now(UTC),
        )
        session.add(art)
        await session.flush()
        session.add(
            Summary(
                article_id=art.id,
                run_id=run.id,
                language="tr",
                title_local=f"Haber {importance}",
                summary="Ozet.",
                why_it_matters="Bu yuzden onemli.",
                tags_json="[]",
                importance=importance,
                # Rank ascends as importance descends: the worst story is first.
                rank=position + 1,
            )
        )
    await session.commit()

    body = client.get("/").text
    assert re.findall(r'class="item p(\d)"', body) == ["5", "4", "3", "2"]


def test_the_expander_reveals_everything_summarised(client: TestClient, digest: Run) -> None:
    assert 'href="/?lang=tr&amp;all=1"' in client.get("/").text
    assert client.get("/?all=1").text.count('class="item ') == 5


def test_an_empty_database_explains_itself(client: TestClient) -> None:
    body = client.get("/").text
    assert "Henüz bülten yok" in body
    assert "Çalışmalar sayfasındaki" in body, "it points at where the press actually is"
    assert 'class="item ' not in body


def test_the_tag_filter_narrows_the_list(client: TestClient, digest: Run) -> None:
    body = client.get("/?all=1&tag=policy").text
    assert body.count('class="item ') == 3
    assert "OpenAI ucuz bir model duyurdu" not in body


# -- language -----------------------------------------------------------------


def test_the_language_toggle_switches_and_is_remembered(client: TestClient, digest: Run) -> None:
    response = client.get("/?lang=en")
    assert response.cookies.get(LANGUAGE_COOKIE) == "en"
    # The label carries an apostrophe, which the template escapes; the shell's
    # own `lang` attribute is the same claim without the encoding.
    assert '<html lang="en">' in response.text


def test_the_switch_translates_the_shell_and_never_hides_the_bulletin(
    client: TestClient, digest: Run
) -> None:
    """The switch is not a content filter (ADR 0017).

    It was one until 2026-09-05: `latest_digest_run` took the page's language,
    so a reader on `?lang=en` with only a Turkish bulletin in the database got
    "No digest yet" - the digest was there, the shell was hiding it.
    """
    body = client.get("/?lang=en").text
    assert "Gunun ortak konusu" in body, "the Turkish bulletin is still the latest one"
    assert "No digest yet" not in body


def test_the_bar_names_the_bulletins_language_only_when_it_differs(
    client: TestClient, digest: Run
) -> None:
    """A Turkish digest under an English shell is allowed, but it is labelled."""
    english = client.get("/?lang=en").text
    assert 'class="bar__lang" lang="tr"' in english

    turkish = client.get("/?lang=tr").text
    assert "bar__lang" not in turkish, "a mark that is always drawn says nothing"


def test_the_remembered_language_survives_a_plain_visit(client: TestClient, digest: Run) -> None:
    client.get("/?lang=en")
    assert '<html lang="en">' in client.get("/").text
    client.get("/?lang=tr")
    assert "Bugünün özeti" in client.get("/").text


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
            error="Ben's Bites: ConnectTimeout",
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


async def test_a_blocked_host_is_refused_before_it_is_probed(
    client: TestClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The block has to short-circuit the probe, not merely undo it afterwards."""
    from ainews.pipeline.nodes import collect

    def explode(url: str) -> None:
        raise AssertionError(f"a blocked host must never be fetched: {url}")

    monkeypatch.setattr(collect, "probe_feed", explode)

    response = client.post(
        "/sources/add",
        data={"url": "https://www.reddit.com/r/LocalLLaMA/.rss", "lang": "tr"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "bad=true" in response.headers["location"]
    assert (await session.execute(select(Source))).scalars().first() is None


# -- static -------------------------------------------------------------------


def test_the_page_loads_no_third_party_assets(client: TestClient, digest: Run) -> None:
    """It is a local tool; it has to open with the network unplugged."""
    body = client.get("/").text
    assert "https://" not in body.split("</head>")[0]


def test_fonts_and_scripts_are_served_locally(client: TestClient) -> None:
    for path in ("/static/theme.css", "/static/fonts.css", "/static/htmx.min.js"):
        assert client.get(path).status_code == 200


# -- the front page and the supplement press (2026-09-08) -----------------------


async def _bulletin(
    session: AsyncSession, *, n: int, minutes_ago: float, note: str, src: Source
) -> Run:
    started = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    run = Run(
        kind="digest",
        language="tr",
        status="ok",
        n_summarized=n,
        editor_note=note,
        started_at=started,
        finished_at=started + timedelta(minutes=1),
    )
    session.add(run)
    await session.flush()
    for i in range(n):
        art = Article(
            source_id=src.id,
            title=f"{note} {i}",
            url=f"https://openai.com/{run.id}/{i}",
            url_canonical=f"https://openai.com/{run.id}/{i}",
            published_at=started,
        )
        session.add(art)
        await session.flush()
        session.add(
            Summary(
                article_id=art.id,
                run_id=run.id,
                language="tr",
                title_local=f"{note} {i}",
                summary="Bir. Iki. Uc.",
                why_it_matters="Onemli.",
                importance=3,
                rank=i + 1,
            )
        )
    await session.commit()
    return run


async def test_a_two_story_press_does_not_replace_the_mornings_bulletin(
    client: TestClient, session: AsyncSession
) -> None:
    """Every press is a delta. A second press ten minutes after a full bulletin
    summarises what arrived in between - two stories - and used to become the
    front page, with the fifteen-story bulletin dropped into the archive."""
    src = Source(name="OpenAI", url="https://openai.com/news/rss.xml")
    session.add(src)
    await session.flush()
    morning = await _bulletin(session, n=15, minutes_ago=30, note="Sabah bulteni", src=src)
    supplement = await _bulletin(session, n=2, minutes_ago=10, note="Ek baski", src=src)

    body = client.get("/").text
    assert "Sabah bulteni" in body and "Ek baski 0" not in body
    assert body.count('class="item ') == 15

    archive = client.get("/archive").text
    assert morning.id in archive and supplement.id in archive, "the supplement is not lost"


async def test_a_small_run_is_the_bulletin_when_the_last_full_one_is_a_day_old(
    client: TestClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule is scoped to one bulletin-day (`digest_suggest_after_hours`):
    on a quiet day two stories is the bulletin, and yesterday's fifteen would
    be a stale front page."""
    monkeypatch.setenv("DIGEST_SUGGEST_AFTER_HOURS", "24")
    get_settings.cache_clear()
    src = Source(name="OpenAI", url="https://openai.com/news/rss.xml")
    session.add(src)
    await session.flush()
    await _bulletin(session, n=15, minutes_ago=26 * 60, note="Dunku bulten", src=src)
    await _bulletin(session, n=2, minutes_ago=10, note="Sakin gun", src=src)

    body = client.get("/").text
    assert "Sakin gun" in body and "Dunku bulten 0" not in body


async def test_the_question_says_how_many_stories_are_waiting(
    client: TestClient, digest: Run, session: AsyncSession
) -> None:
    """The size of the delta, before it is paid for."""
    assert "özetlenmeyi bekleyen haber yok" in client.get("/runs/confirm?lang=tr").text

    src = (await session.execute(select(Source))).scalars().first()
    session.add(
        Article(
            source_id=src.id,
            title="Yeni gelen",
            url="https://openai.com/news/new",
            url_canonical="https://openai.com/news/new",
            published_at=datetime.now(UTC),
        )
    )
    await session.commit()
    asked = client.get("/runs/confirm?lang=tr").text
    assert "özetlenmeyi bekleyen 1 haber var" in asked
    assert "bekleyen" not in client.get("/runs/action?lang=tr").text, "only inside the question"


async def test_a_partial_run_is_not_drawn_as_a_failure(
    client: TestClient, digest: Run, session: AsyncSession
) -> None:
    """One feed 404ed and ninety-one summaries shipped; `persist` calls that a
    real outcome and not a failure, and the run log used to paint it in the
    alarm colour anyway. The dead feed stays readable under the pointer."""
    session.add(
        Run(
            kind="digest",
            language="tr",
            status="partial",
            n_summarized=91,
            error="Ben's Bites: HTTP 404",
            finished_at=datetime.now(UTC),
        )
    )
    await session.commit()
    body = client.get("/runs").text
    assert "Kısmi" in body
    assert 'class="bad"' not in body
    assert 'title="Ben&#39;s Bites: HTTP 404"' in body or 'title="Ben\'s Bites: HTTP 404"' in body


async def test_the_page_draws_the_editors_score_where_the_ranker_gave_one(
    client: TestClient, session: AsyncSession
) -> None:
    """ADR 0025: the summariser said 3 for every story; the ranker, seeing the
    day, said 5, 4, 2 - and the headline sizes and the order follow the ranker.
    A story below the fold keeps the summariser's score."""
    src = Source(name="OpenAI", url="https://openai.com/news/rss.xml")
    session.add(src)
    await session.flush()
    run = Run(
        kind="digest", language="tr", status="ok", n_summarized=4, finished_at=datetime.now(UTC)
    )
    session.add(run)
    await session.flush()
    for position, (editor, rank) in enumerate([(2, 3), (5, 1), (4, 2), (None, None)]):
        art = Article(
            source_id=src.id,
            title=f"Haber {position}",
            url=f"https://openai.com/news/e{position}",
            url_canonical=f"https://openai.com/news/e{position}",
            published_at=datetime.now(UTC),
        )
        session.add(art)
        await session.flush()
        session.add(
            Summary(
                article_id=art.id,
                run_id=run.id,
                language="tr",
                title_local=f"Haber {position}",
                summary="Ozet.",
                why_it_matters="Bu yuzden onemli.",
                tags_json="[]",
                importance=3,
                editor_importance=editor,
                rank=rank,
            )
        )
    await session.commit()

    assert re.findall(r'class="item p(\d)"', client.get("/").text) == ["5", "4", "2"]
    assert re.findall(r'class="item p(\d)"', client.get("/?all=1").text) == ["5", "4", "3", "2"]
