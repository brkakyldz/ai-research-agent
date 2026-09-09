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
from tests.factories import publish

from ainews.db import Article, Bulletin, BulletinItem, Run, Source, Summary
from ainews.web.i18n import LANGUAGE_COOKIE

# -- the digest page ----------------------------------------------------------


def test_the_digest_shows_the_note_and_the_ranked_stories(
    client: TestClient, digest: Bulletin
) -> None:
    body = client.get("/").text
    assert "Gunun ortak konusu" in body
    assert body.count('class="item ') == 3, "only ranked stories are above the fold"
    assert "OpenAI ucuz bir model duyurdu" in body


def test_importance_drives_the_class_that_drives_the_type(
    client: TestClient, digest: Bulletin
) -> None:
    """DESIGN.md's central claim: the score is visible only through typography."""
    body = client.get("/").text
    assert 'class="item p5"' in body
    assert 'class="item p4"' in body
    # No badge, no rank number - the whole point of the removals.
    assert "importance" not in body.lower()


async def test_the_bulletin_reads_in_the_order_the_editor_put_it_in(
    client: TestClient, session: AsyncSession
) -> None:
    """`position` and only `position` (ADR 0030). The tier is what the
    typography draws, not what the list sorts by - two majors and a notable can
    read major, notable, major if that is the day's argument, and sorting by
    tier would rewrite the editor's order into groups."""
    src = Source(name="OpenAI", url="https://openai.com/news/rss.xml")
    session.add(src)
    await session.flush()

    ids: list[int] = []
    for position in range(4):
        art = Article(
            source_id=src.id,
            title=f"Haber {position}",
            url=f"https://openai.com/news/o{position}",
            url_canonical=f"https://openai.com/news/o{position}",
            published_at=datetime.now(UTC),
        )
        session.add(art)
        await session.flush()
        summary = Summary(
            article_id=art.id,
            language="tr",
            title_local=f"Haber {position}",
            summary="Ozet.",
            why_it_matters="Bu yuzden onemli.",
            tags_json="[]",
            # The summariser's own scores run the other way, and the page must
            # not read them: inside a bulletin the step is the editor's tier.
            importance=[2, 5, 3, 4][position],
        )
        session.add(summary)
        await session.flush()
        ids.append(summary.id)
    await session.commit()
    await publish(session, ids, tiers=["lead", "major", "notable", "brief"])

    body = client.get("/").text
    assert re.findall(r'class="item p(\d)"', body) == ["5", "4", "3", "2"]
    assert body.index("Haber 0") < body.index("Haber 1") < body.index("Haber 2")


def test_the_expander_reveals_everything_summarised(client: TestClient, digest: Bulletin) -> None:
    assert 'href="/?lang=tr&amp;all=1"' in client.get("/").text
    assert client.get("/?all=1").text.count('class="item ') == 5


async def test_an_irrelevant_summary_is_in_neither_list(
    client: TestClient, session: AsyncSession, digest: Bulletin
) -> None:
    """The relevance call is the system's only "is this AI news" filter (ADR
    0031), and one feed is a personal blog that also publishes on map
    projections. The bulletin never sees it, and neither does the block under
    the bulletin - being outside the editor's picks is not the same as being
    outside the subject."""
    src = (await session.execute(select(Source))).scalars().first()
    assert src is not None
    art = Article(
        source_id=src.id,
        title="Mercator projeksiyonu uzerine",
        url="https://example.com/maps",
        url_canonical="https://example.com/maps",
        published_at=datetime.now(UTC),
    )
    session.add(art)
    await session.flush()
    session.add(
        Summary(
            article_id=art.id,
            language="tr",
            title_local="Mercator projeksiyonu uzerine",
            summary="Bir. Iki. Uc.",
            why_it_matters="Onemli degil.",
            importance=2,
            relevant=False,
        )
    )
    await session.commit()

    body = client.get("/?all=1").text
    assert body.count('class="item ') == 5, "the same five, and not a sixth"
    assert "Mercator" not in body


def test_an_empty_database_explains_itself(client: TestClient) -> None:
    body = client.get("/").text
    assert "Henüz bülten yok" in body
    assert "Çalışmalar sayfasındaki" in body, "it points at where the press actually is"
    assert 'class="item ' not in body


def test_the_tag_filter_narrows_the_list(client: TestClient, digest: Bulletin) -> None:
    body = client.get("/?all=1&tag=policy").text
    assert body.count('class="item ') == 3
    assert "OpenAI ucuz bir model duyurdu" not in body


# -- language -----------------------------------------------------------------


def test_the_language_toggle_switches_and_is_remembered(
    client: TestClient, digest: Bulletin
) -> None:
    response = client.get("/?lang=en")
    assert response.cookies.get(LANGUAGE_COOKIE) == "en"
    # The label carries an apostrophe, which the template escapes; the shell's
    # own `lang` attribute is the same claim without the encoding.
    assert '<html lang="en">' in response.text


def test_the_switch_translates_the_shell_and_never_hides_the_bulletin(
    client: TestClient, digest: Bulletin
) -> None:
    """The switch is not a content filter (ADR 0017).

    As one, it hides the reading: `latest_digest_run` taking the page's
    language gives a reader on `?lang=en` with only a Turkish bulletin in the
    database "No digest yet" - the digest is there, the shell is hiding it.
    """
    body = client.get("/?lang=en").text
    assert "Gunun ortak konusu" in body, "the Turkish bulletin is still the latest one"
    assert "No digest yet" not in body


def test_the_bar_names_the_bulletins_language_only_when_it_differs(
    client: TestClient, digest: Bulletin
) -> None:
    """A Turkish digest under an English shell is allowed, but it is labelled."""
    english = client.get("/?lang=en").text
    assert 'class="bar__lang" lang="tr"' in english

    turkish = client.get("/?lang=tr").text
    assert "bar__lang" not in turkish, "a mark that is always drawn says nothing"


def test_the_remembered_language_survives_a_plain_visit(
    client: TestClient, digest: Bulletin
) -> None:
    client.get("/?lang=en")
    assert '<html lang="en">' in client.get("/").text
    client.get("/?lang=tr")
    assert "Bugünün özeti" in client.get("/").text


def test_the_toggle_link_keeps_the_rest_of_the_query(client: TestClient, digest: Bulletin) -> None:
    body = client.get("/?all=1&tag=policy").text
    assert "all=1" in body and "tag=policy" in body and "lang=en" in body


# -- search -------------------------------------------------------------------


def test_search_finds_a_summary(client: TestClient, digest: Bulletin) -> None:
    assert "Ozet metni 0" in client.get("/search?q=Ozet").text


def test_search_matches_a_turkish_suffix(client: TestClient, digest: Bulletin) -> None:
    """Prefix matching is what makes the box work in an agglutinative language."""
    assert client.get("/search?q=model").text.count('class="item ') >= 1


def test_search_operators_do_not_crash_the_page(client: TestClient, digest: Bulletin) -> None:
    """FTS5 raises on a bare AND or a stray quote; the box must not."""
    for query in ("model AND", '"', "a - b", "NEAR(", "*"):
        assert client.get("/search", params={"q": query}).status_code == 200


def test_an_empty_search_is_not_an_error(client: TestClient, digest: Bulletin) -> None:
    assert client.get("/search?q=").status_code == 200


# -- archive, runs, sources ---------------------------------------------------


def test_the_archive_lists_and_renders_a_past_run(client: TestClient, digest: Bulletin) -> None:
    body = client.get("/archive").text
    assert "Gunun ortak konusu" in body
    assert body.count('class="item ') == 3


def test_the_runs_page_shows_cost_and_duration(client: TestClient, digest: Bulletin) -> None:
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


def test_the_page_loads_no_third_party_assets(client: TestClient, digest: Bulletin) -> None:
    """It is a local tool; it has to open with the network unplugged."""
    body = client.get("/").text
    assert "https://" not in body.split("</head>")[0]


def test_fonts_and_scripts_are_served_locally(client: TestClient) -> None:
    for path in ("/static/theme.css", "/static/fonts.css", "/static/htmx.min.js"):
        assert client.get(path).status_code == 200


# -- the front page and the supplement press (2026-09-08) -----------------------


async def _bulletin(
    session: AsyncSession,
    *,
    n: int,
    minutes_ago: float,
    note: str,
    src: Source,
    version: int = 1,
    keep: Bulletin | None = None,
) -> Bulletin:
    """One press, published as a version of today.

    `keep` is the version this one replaces: a press re-ranks the whole day, so
    version 2 carries version 1's stories as well as the ones it just bought.
    """
    started = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    run = Run(
        kind="digest",
        language="tr",
        status="ok",
        n_summarized=n,
        started_at=started,
        finished_at=started + timedelta(minutes=1),
    )
    session.add(run)
    await session.flush()
    ids: list[int] = []
    if keep is not None:
        ids.extend(
            (
                await session.execute(
                    select(BulletinItem.summary_id)
                    .where(BulletinItem.bulletin_id == keep.id)
                    .order_by(BulletinItem.position)
                )
            )
            .scalars()
            .all()
        )
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
        summary = Summary(
            article_id=art.id,
            language="tr",
            title_local=f"{note} {i}",
            summary="Bir. Iki. Uc.",
            why_it_matters="Onemli.",
            importance=3,
        )
        session.add(summary)
        await session.flush()
        ids.append(summary.id)
    await session.commit()
    return await publish(session, ids, version=version, editor_note=note, run=run)


async def test_a_second_press_republishes_the_day_rather_than_supplementing_it(
    client: TestClient, session: AsyncSession
) -> None:
    """Every press is a delta, and used to publish one: a press ten minutes
    after a full bulletin summarised the two stories that arrived in between and
    became the front page, with the morning's fifteen dropped into the archive.

    It writes a new version of the same day now (ADR 0030), so the front page is
    seventeen stories and the archive keeps what the reader was shown at nine.
    """
    src = Source(name="OpenAI", url="https://openai.com/news/rss.xml")
    session.add(src)
    await session.flush()
    morning = await _bulletin(session, n=15, minutes_ago=30, note="Sabah bulteni", src=src)
    later = await _bulletin(
        session, n=2, minutes_ago=10, note="Ek baski", src=src, version=2, keep=morning
    )

    body = client.get("/").text
    assert body.count('class="item ') == 17
    assert "Sabah bulteni 0" in body and "Ek baski 0" in body

    archive = client.get("/archive").text
    assert f"b={morning.id}" in archive and f"b={later.id}" in archive
    assert "v2" in archive, "the version is on the row, so a reader can tell them apart"

    superseded = client.get(f"/archive?b={morning.id}").text
    assert "daha yeni bir sürümü" in superseded
    assert superseded.count('class="item ') == 15


async def test_the_two_scales_are_never_drawn_in_one_list(
    client: TestClient, session: AsyncSession
) -> None:
    """A bulletin item draws its tier; anything outside the bulletin draws the
    summariser's own significance; nothing coalesces (ADR 0030). The failure
    that rule prevents is a ranked 3-that-became-5 drawn larger than an honest
    unranked 3, in a layout whose only ranking indicator is size."""
    src = Source(name="OpenAI", url="https://openai.com/news/rss.xml")
    session.add(src)
    await session.flush()
    ids: list[int] = []
    for position in range(4):
        art = Article(
            source_id=src.id,
            title=f"Haber {position}",
            url=f"https://openai.com/news/e{position}",
            url_canonical=f"https://openai.com/news/e{position}",
            published_at=datetime.now(UTC),
        )
        session.add(art)
        await session.flush()
        summary = Summary(
            article_id=art.id,
            language="tr",
            title_local=f"Haber {position}",
            summary="Ozet.",
            why_it_matters="Bu yuzden onemli.",
            tags_json="[]",
            importance=3,
        )
        session.add(summary)
        await session.flush()
        if position < 3:
            ids.append(summary.id)
    await session.commit()
    await publish(session, ids, tiers=["lead", "major", "brief"])

    # The bulletin, at the editor's tiers.
    assert re.findall(r'class="item p(\d)"', client.get("/").text) == ["5", "4", "2"]
    # And under it, the story the editor left out, at the summariser's own 3.
    assert re.findall(r'class="item p(\d)"', client.get("/?all=1").text) == [
        "5",
        "4",
        "2",
        "3",
    ]
