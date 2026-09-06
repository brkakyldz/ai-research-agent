"""Page tests.

Every page is rendered from real rows rather than from a fixture of HTML, so a
template that references a field the query stopped returning fails here rather
than at seven in the morning.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ainews.config import Settings
from ainews.db import Article, Run, Source, Summary
from ainews.web.app import create_app
from ainews.web.i18n import LANGUAGE_COOKIE, THEME_COOKIE
from ainews.web.views import MONTHS, format_stamp, split_paragraphs

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


# -- run now ------------------------------------------------------------------


def test_run_now_refuses_without_a_key(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from ainews.config import get_settings

    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    with TestClient(create_app(get_settings())) as fresh:
        body = fresh.post("/runs/start?lang=tr").text
    assert "OPENAI_API_KEY" in body


def test_run_now_starts_one_run_and_refuses_a_second(
    settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two presses must not become two runs competing for the same candidates.

    This one builds its own client inside a `with`, unlike the shared fixture.
    Without the context manager TestClient closes its event loop after every
    response; that destroys the pending digest task, which runs `_guarded`'s
    `finally` and releases the claim - so the second press would look free for a
    reason that has nothing to do with the application.
    """
    import asyncio

    from ainews.web.routes import runs as runs_route

    started = 0

    async def _never_finishes(language: str, models: tuple[str, str]) -> None:
        """Stands in for a two-minute digest, so the claim is still held.

        It deliberately never returns. Sleeping for a fixed time instead would
        make the assertion a race: `_guarded` releases the claim as soon as this
        returns, and the second request takes longer to arrive than any sleep
        short enough to keep the test fast.
        """
        nonlocal started
        started += 1
        await asyncio.Event().wait()

    monkeypatch.setattr(runs_route, "_execute", _never_finishes)

    with TestClient(create_app(settings)) as client:
        first = client.post("/runs/start?lang=tr")
        second = client.post("/runs/start?lang=tr")

    assert "Çalışıyor" in first.text
    assert "Zaten" in second.text
    assert started == 1


# -- static -------------------------------------------------------------------


def test_the_page_loads_no_third_party_assets(client: TestClient, digest: Run) -> None:
    """It is a local tool; it has to open with the network unplugged."""
    body = client.get("/").text
    assert "https://" not in body.split("</head>")[0]


def test_fonts_and_scripts_are_served_locally(client: TestClient) -> None:
    for path in ("/static/theme.css", "/static/fonts.css", "/static/htmx.min.js"):
        assert client.get(path).status_code == 200


@pytest.fixture(autouse=True)
def _no_run_claim_leaks() -> Iterator[None]:
    """Clear the module-level run claim around every test.

    `_running` lives for the life of the process, and a fire-and-forget task that
    the test's event loop tears down before it finishes leaves its token behind.
    The next test then sees a run in flight that does not exist.
    """
    from ainews.pipeline import runner

    runner._digest_running.clear()
    yield
    runner._digest_running.clear()


async def test_two_simultaneous_presses_start_one_run(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard has to hold when both presses are genuinely in flight at once.

    `TestClient` cannot show this: its calls block, so the first task always gets
    to run before the second request is made. Only two coroutines awaited together
    reach the window between `create_task` scheduling the task and the task's own
    first line - the window a fire-and-forget run has to claim its slot before.
    """
    import asyncio

    from ainews.pipeline import runner
    from ainews.web.routes import runs as runs_route

    started: list[str] = []

    async def _fake_digest(
        language: str | None = None,
        mode: str | None = None,
        model_summarize: str | None = None,
        model_rank: str | None = None,
    ) -> str:
        started.append(language or "")
        await asyncio.sleep(0.2)
        return "fake-run-id"

    monkeypatch.setattr(runner, "run_digest", _fake_digest)
    request = SimpleNamespace(cookies={}, query_params={})

    first, second = await asyncio.gather(
        runs_route.start_run(request, lang="tr", settings=settings),  # type: ignore[arg-type]
        runs_route.start_run(request, lang="tr", settings=settings),  # type: ignore[arg-type]
    )
    bodies = {first.body.decode(), second.body.decode()}

    await asyncio.sleep(0.4)
    assert started == ["tr"], "a second press must not become a second paid run"
    assert any("hx-get" in b for b in bodies), "one press must start the run"
    assert any("hx-get" not in b for b in bodies), "the other must be refused as busy"
    assert not runs_route.is_running(), "the claim must be released when the run ends"


# -- the shell ----------------------------------------------------------------


PAGES = ["/", "/search", "/archive", "/sources", "/runs"]


@pytest.mark.parametrize("path", PAGES)
def test_every_page_wears_the_rail(client: TestClient, digest: Run, path: str) -> None:
    """The rail is the only way between five pages, so its absence on any one of
    them is a dead end, not a cosmetic problem."""
    body = client.get(path).text
    assert '<aside class="rail"' in body
    for target in PAGES:
        href = "/?lang=" if target == "/" else f'href="{target}?lang='
        assert href in body, f"{path} does not link to {target}"


@pytest.mark.parametrize("path", PAGES)
def test_the_rail_marks_the_page_you_are_on(client: TestClient, digest: Run, path: str) -> None:
    body = client.get(path).text
    marked = [line for line in body.splitlines() if 'aria-current="page"' in line]
    # The runs link is in the markup twice - once in the foot, once in the list
    # the phone draws - so on `/runs` two lines carry the mark and CSS shows one
    # of them. What must never happen is two *different* destinations claiming
    # to be the page you are on, so the count that matters is of hrefs.
    hrefs = {line.split('href="')[1].split('"')[0] for line in marked}
    assert len(hrefs) == 1, "exactly one destination is current"
    assert len(marked) == (2 if path == "/runs" else 1)
    assert f'href="{path}?lang=tr"' in marked[0]


def test_the_rail_counts_what_is_behind_each_link(client: TestClient, digest: Run) -> None:
    """The badge counts the stories the link leads to, and it is a COUNT.

    It was `min(run.n_summarized, digest_top_n)` until 2026-09-06 - a ceiling
    the ranker is asked to respect, not a promise it made. The seeded run
    summarises five and ranks three, so the badge said five over a page of
    three; a real run on 2026-09-05 said fifteen over a page of eleven.
    """
    body = client.get("/").text
    assert '<span class="pill pill--acc">3</span>' in body, "three ranked stories today"
    assert '<span class="pill">1</span>' in body, "one digest run in the archive"
    assert body.count('class="item ') == 3, "and the badge is the length of the list"
    assert "3 haber · " in body, "the brief's footnote is the same number"


def test_the_rail_carries_the_advice_not_a_clock(client: TestClient, digest: Run) -> None:
    """Nothing fires on a schedule any more (ADR 0015), so the rail's foot says
    when a run would be worth starting. The seeded digest is minutes old and the
    suggested gap is a day, so the foot counts down rather than saying "now"."""
    body = client.get("/").text
    assert "Önerilen çalıştırma" in body
    assert "sonra</span>" in body, "a countdown, not the scheduler's next fire time"


def test_the_run_page_leads_with_the_advice(client: TestClient, digest: Run) -> None:
    """The block that replaced the cron: a state, a reason, and three facts."""
    body = client.get("/runs").text
    assert 'data-state="waiting"' in body
    assert "Bugünün bülteni alındı" in body, "the state line"
    assert "Önerilen zaman" in body and "Kaynak taraması" in body, "two of the three facts"
    assert "data-due=" in body, "and a countdown the script can keep ticking"


async def test_a_stale_digest_turns_the_block_into_an_invitation(
    client: TestClient, session: AsyncSession, digest: Run
) -> None:
    """The state a reader meets on any morning after the first.

    The seeded digest is minutes old, so it is aged past the suggested gap here
    rather than in a second fixture - what changes on the page is the sentence
    and the countdown's sign, and nothing else moves.
    """
    digest.started_at = datetime.now(UTC) - timedelta(hours=30)
    session.add(digest)
    await session.commit()

    body = client.get("/runs").text

    assert 'data-state="due"' in body
    assert "Çalıştırmaya hazır" in body
    assert "önerilen aralık 24 saat" in body, "the reason names the rule it applied"
    assert "önce</p>" in body, "the countdown counts up once the time has passed"
    # The button is not disabled: being late is not a reason to refuse the press.
    # At rest the page has no `hx-disabled-elt` either - that lives on the answer
    # inside the question, which is not rendered until the button is pressed.
    assert "disabled" not in body


@pytest.mark.parametrize("path", ["/", "/sources", "/archive", "/search"])
def test_only_the_run_page_can_start_a_run(path: str, client: TestClient, digest: Run) -> None:
    """The press is on `/runs` and nowhere else.

    It was in the top bar of every page until 2026-09-06 and briefly in the
    rail's foot after that. What every other page carries now is the way in -
    the runs link, drawn in the accent - and nothing that spends money.
    """
    body = client.get(path).text
    assert "/runs/start" not in body and "/runs/confirm" not in body
    assert body.count('class="nav--go"') == 1, "the accented link is in the foot"
    assert body.count('class="nav--narrow"') == 1, "and once more for the phone"


def test_the_press_asks_before_it_spends(client: TestClient, digest: Run) -> None:
    """One click gets you a question, not a run.

    The resting button is a `GET` of the question; only the answer is a `POST`.
    That is the whole guarantee, and it is visible in the markup: `/runs` at rest
    contains no `hx-post` at all.
    """
    body = client.get("/runs").text
    assert 'hx-get="/runs/confirm' in body
    assert "hx-post" not in body, "nothing on the resting page posts anything"

    asked = client.get("/runs/confirm").text
    assert 'hx-post="/runs/start' in asked, "the answer is the only thing that posts"
    assert "Evet, çalıştır" in asked and "Vazgeç" in asked
    # It says what the press will do and what the last one cost, and it does not
    # restate the state line above it - that sentence is on the page already.
    assert "kaynak taranacak" in asked
    assert "Son çalışma $0.042 tuttu." in asked
    assert "bülteni alındı" not in asked, "the question does not repeat the state line"

    # `vazgeç` puts the button back, and that path is a GET too.
    assert 'hx-get="/runs/action' in asked
    assert 'hx-get="/runs/confirm' in client.get("/runs/action").text


def test_the_question_carries_the_output_language_and_starts_on_the_pages(
    client: TestClient, digest: Run
) -> None:
    """Which language the bulletin is written in is asked at the press.

    It used to be read off the shell's TR/EN switch, so a reader who wanted
    English buttons also, silently, bought an English bulletin (ADR 0017).
    """
    asked = client.get("/runs/confirm?lang=tr").text
    assert 'class="seg seg--out"' in asked
    assert 'aria-current="true"' in asked
    assert "out=tr" in asked and "out=en" in asked
    assert (
        'hx-post="/runs/start?lang=tr&amp;out=tr&amp;ms=gpt-5.6-luna&amp;mr=gpt-5.6-luna"' in asked
    ), "it starts on the page's language and the configured models"

    # Choosing the other slot is the same swap the question itself is: a GET of
    # this fragment with the other `out`, no client state anywhere.
    other = client.get("/runs/confirm?lang=tr&out=en").text
    assert "&amp;out=en&amp;" in other
    assert "Evet, çalıştır" in other, "the shell is still Turkish"


def test_the_press_produces_the_language_it_was_asked_for(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, engine: AsyncEngine
) -> None:
    """`?out=` reaches the pipeline, and `?lang=` never does."""
    from ainews.web.routes import runs as runs_route

    settings.openai_api_key = "sk-test"
    produced: list[str] = []

    async def _record(language: str, models: tuple[str, str]) -> None:
        produced.append(language)

    monkeypatch.setattr(runs_route, "_execute", _record)

    with TestClient(create_app(settings)) as client:
        client.post("/runs/start?lang=tr&out=en")

    assert produced == ["en"], "the shell's language is not what gets written"


def test_the_first_run_is_told_what_it_will_do(
    client: TestClient, engine: AsyncEngine, settings: Settings
) -> None:
    """With no digest behind it the question cannot talk about repeating one, so
    it describes the work instead - and says nothing about cost, because there
    is no measured number to say."""
    asked = client.get("/runs/confirm").text
    assert "kaynak taranacak" in asked
    assert "tuttu" not in asked, "no invented estimate before the first run"


def test_search_finds_a_story_whatever_the_shell_is_set_to(client: TestClient, digest: Run) -> None:
    """The index is not filtered by the page's language either (ADR 0017).

    A reader looking for a company name wants the article; refusing it because
    the bulletin covering it was written in the other language is the same
    content filter the switch stopped being.
    """
    body = client.get("/search?q=OpenAI&lang=en").text
    assert "OpenAI ucuz bir model duyurdu" in body


def test_the_archive_lists_every_bulletin_and_names_the_odd_ones_out(
    client: TestClient, digest: Run
) -> None:
    body = client.get("/archive?lang=en").text
    assert "Gunun ortak konusu" in body, "a Turkish bulletin is still in the archive"
    assert '<span lang="tr">Türkçe</span>' in body, "and its pill says which language it is"


# -- the theme ----------------------------------------------------------------


def test_the_default_theme_writes_no_attribute(client: TestClient, digest: Run) -> None:
    """No `data-theme` is the follow-the-system state: `color-scheme: light dark`
    does the work, and an attribute would override the reader's own setting."""
    body = client.get("/").text
    assert "data-theme" not in body
    assert client.cookies.get(THEME_COOKIE) == "system"


@pytest.mark.parametrize("value", ["light", "dark"])
def test_a_chosen_theme_is_rendered_and_remembered(
    client: TestClient, digest: Run, value: str
) -> None:
    response = client.get(f"/?theme={value}")
    assert f'data-theme="{value}"' in response.text
    assert response.cookies.get(THEME_COOKIE) == value
    # The cookie alone has to carry it to the next page, with no query string.
    assert f'data-theme="{value}"' in client.get("/runs").text


def test_a_nonsense_theme_falls_back_to_the_system(client: TestClient, digest: Run) -> None:
    assert "data-theme" not in client.get("/?theme=neon").text


def test_the_theme_links_keep_the_rest_of_the_query(client: TestClient, digest: Run) -> None:
    body = client.get("/?all=1&tag=policy").text
    assert "all=1" in body and "tag=policy" in body and "theme=dark" in body


# -- the story block ----------------------------------------------------------


def test_a_story_names_its_impact_band(client: TestClient, digest: Run) -> None:
    """The bars are a shape; the word beside them is what makes it a scale."""
    body = client.get("/").text
    assert "Yüksek etki" in body, "the 5 and the 4 are the high band"
    assert "Orta etki" in body, "the 3 is the middle one"


def test_the_impact_meter_lights_one_bar_per_step_of_the_band(
    client: TestClient, digest: Run
) -> None:
    """Three bars, lit up to the band, and the band names the colour.

    Five bars driven by the raw score said something finer than the word beside
    them did; three lit by the same three steps say one thing.
    """
    body = client.get("/").text
    high = '<span class="score score--high">'
    mid = '<span class="score score--mid">'
    assert high in body and mid in body
    lit_all = '<i class="f"></i><i class="f"></i><i class="f"></i>'
    lit_two = '<i class="f"></i><i class="f"></i><i class=""></i>'
    assert lit_all in body[body.index(high) :]
    assert lit_two in body[body.index(mid) :]


def test_why_it_matters_is_labelled_and_separate(client: TestClient, digest: Run) -> None:
    """It is the one thing here an RSS reader does not have, so it is not left
    as bold text inside the summary."""
    body = client.get("/").text
    assert "Neden önemli" in body
    assert '<div class="why">' in body


def test_a_story_carries_its_topics_and_they_filter(client: TestClient, digest: Run) -> None:
    body = client.get("/").text
    assert 'class="chip" href="/?lang=tr&amp;all=1&amp;tag=openai"' in body
    # And following one narrows the list, which is the only reason it is a link.
    assert client.get("/?all=1&tag=openai").text.count('class="item ') == 2


def test_a_source_keeps_its_own_capitals(client: TestClient, digest: Run) -> None:
    """It was lowercased to `openai`, which is the one word on the meta row a
    reader recognises at a glance."""
    body = client.get("/").text
    assert "</span>OpenAI" in body
    assert "</span>openai" not in body


def test_every_story_offers_its_source(client: TestClient, digest: Run) -> None:
    body = client.get("/").text
    assert body.count('class="go"') == 3, "one per story above the fold"
    assert "Kaynağa git" in body


def test_a_low_scoring_story_is_only_a_headline(client: TestClient, digest: Run) -> None:
    """Ranks 2 and 1 collapse in CSS, so the markup has to stay identical -
    the test is that they are still whole items, not truncated ones."""
    body = client.get("/?all=1").text
    assert body.count('class="item ') == 5
    assert body.count('<div class="why">') == 5


# -- the header and the rail --------------------------------------------------


def test_execution_metrics_are_off_the_reading_page_entirely(
    client: TestClient, digest: Run
) -> None:
    """Cost is not deleted, it is placed - and the place is `/runs`.

    It came off the top bar on 2026-09-05 and off the digest's side column on
    2026-09-06, on the same argument both times: it says how the page was made,
    not what is on it. The digest keeps one line about the machine, which is
    whether the last run worked.
    """
    body = client.get("/").text
    assert "$0.042" not in body, "spend belongs to the run log"
    assert "$0.042" in client.get("/runs").text
    assert "Tamam" in body.split('class="side"', 1)[1], "the status line stays"


def test_the_side_column_leads_with_the_day_not_the_machine(
    client: TestClient, digest: Run
) -> None:
    """The column's order is its argument: the note, the themes, the weight of
    the day, the week, and the machine last."""
    side = client.get("/").text.split('class="side"', 1)[1]
    order = [
        side.index("brief--side"),
        side.index(">Günün konuları<"),
        side.index(">Etki dağılımı<"),
        side.index(">Son 7 gün<"),
        side.index('class="status"'),
    ]
    assert order == sorted(order)


def test_every_count_is_a_count_of_the_list_you_can_reach(client: TestClient, digest: Run) -> None:
    """The filter pill, the themes panel and the feed have to agree.

    They did not until 2026-09-06: the tags were counted over every summary the
    run produced while the filter narrowed the ranked ones, so a pill could
    promise 27 stories on a page holding fifteen and return four when pressed.

    Seeded: five summaries, three of them ranked. `openai` is on the first two
    (both ranked), `policy` on the last three (one ranked).
    """
    body = client.get("/").text
    assert "openai <b>2</b>" in body and "policy <b>1</b>" in body
    assert 'style="width: 67%"' in body, "openai is on two of the three ranked"
    assert 'style="width: 33%"' in body, "policy is on one of them"
    # And the promise holds when pressed.
    assert client.get("/?tag=openai").text.count('class="item ') == 2
    assert client.get("/?tag=policy").text.count('class="item ') == 1


def test_showing_everything_widens_the_counts_with_the_list(
    client: TestClient, digest: Run
) -> None:
    """`all=1` is the same agreement over a longer list, not a different rule."""
    body = client.get("/?all=1").text
    assert "openai <b>2</b>" in body and "policy <b>3</b>" in body
    assert 'style="width: 40%"' in body, "openai is on two of five"
    assert 'style="width: 60%"' in body, "policy is on three of five"
    assert body.count('class="item ') == 5


def test_the_briefs_footnote_counts_topics_rather_than_reporting_a_cap(
    client: TestClient, digest: Run
) -> None:
    """The filter row draws at most twelve topics; the footnote counts all of
    them. It used to count the drawn ones, so on any real day it printed the
    cap - `12 konu` - as though it were a measurement."""
    # The seeded ranked three carry `openai`, `models` and `policy`.
    assert "3 haber · 1 kaynak · 3 konu" in client.get("/").text
    # And it stays the size of the digest when the expander widens the list:
    # the two unranked stories carry `policy`, which is already counted.
    assert "3 haber · 1 kaynak · 3 konu" in client.get("/?all=1").text


def test_nothing_rises_without_a_week_behind_it(client: TestClient, digest: Run) -> None:
    """The one derived claim on the page must not fire on a database with a
    single run in it - with no baseline, every topic would look like a trend."""
    assert 'class="rise"' not in client.get("/").text


def test_the_impact_spread_keeps_all_three_bands(client: TestClient, digest: Run) -> None:
    """Three ranked stories: one 5, one 4, one 3 - so two high, one mid, no low.
    The empty band keeps its row, dimmed, because a scale with holes in it is
    still read as a scale."""
    side = client.get("/").text.split('class="side"', 1)[1]
    assert side.count('<li class="score--') == 3
    assert 'class="score--low is-off"' in side


def test_the_side_column_is_read_from_real_runs(client: TestClient, digest: Run) -> None:
    """Nothing here is projected or filled in - if the numbers were invented the
    panel would not be worth the space it took."""
    body = client.get("/").text
    assert 'class="chart"' in body
    assert body.count('class="chart__b"') == 7, "seven local days"
    # The seeded run summarised five stories today, so today's bar is the tallest.
    assert 'style="height: 100%"' in body
    assert '<li class="on"' in body
    assert "16 kaynak açık" in body or "1 kaynak açık" in body


def test_no_story_is_dressed_differently_for_being_first(client: TestClient, digest: Run) -> None:
    """Size on this page means importance and nothing else. The first story used
    to carry a `data-lead` marker and a plate; two ranking systems in one column
    left the reader unable to tell size-by-score from size-by-position, so the
    marker is gone from every list (ADR 0014)."""
    for path in ("/", "/archive", "/search?q=Ozet"):
        assert "data-lead" not in client.get(path).text, path


async def test_the_side_column_survives_an_empty_database(
    client: TestClient, session: AsyncSession
) -> None:
    """The emptiest page is exactly when a reader wants to know whether anything
    has ever run, so the column renders with no runs behind it."""
    body = client.get("/").text
    assert "Henüz çalışma yok" in body
    assert 'class="chart"' in body
    assert 'style="height: 0%"' in body


def test_the_rail_groups_its_five_links(client: TestClient, digest: Run) -> None:
    body = client.get("/").text
    for label in ("Günlük", "Keşfet", "Kayıt"):
        assert f'<p class="nav__g">{label}</p>' in body


def test_the_keyboard_can_skip_the_shell(client: TestClient, digest: Run) -> None:
    body = client.get("/").text
    assert '<a class="skip" href="#main">' in body
    assert 'id="main"' in body


def test_the_language_switch_draws_both_languages_and_marks_the_current_one(
    client: TestClient, digest: Run
) -> None:
    """Both slots, current one marked.

    It was one link labelled with the language you would *get*, which leaves the
    reader deciding whether `english` is the state or the destination.
    """
    body = client.get("/").text
    assert 'class="seg seg--lang"' in body
    assert 'hreflang="tr"' in body
    assert 'hreflang="en"' in body
    assert '<a href="/?lang=tr" lang="tr" hreflang="tr" aria-current="true">' in body


def test_the_language_switch_marks_english_when_english_is_on(
    client: TestClient, digest: Run
) -> None:
    body = client.get("/?lang=en").text
    assert '<a href="/?lang=en" lang="en" hreflang="en" aria-current="true">' in body


def test_the_stamp_names_the_month_in_the_pages_language() -> None:
    """A Turkish page used to read "04 sep": the C locale's month, lower-cased."""
    moment = datetime(2026, 9, 4, 11, 18, tzinfo=UTC)
    assert "Eyl" in format_stamp(moment, "tr")
    assert "Sep" in format_stamp(moment, "en")
    assert format_stamp(None, "tr") == "—"


def test_the_bar_stamp_follows_the_language(client: TestClient, digest: Run) -> None:
    tr = client.get("/?lang=tr").text
    en = client.get("/?lang=en").text
    month = datetime.now(UTC).month - 1
    assert MONTHS["tr"][month] in tr
    assert MONTHS["en"][month] in en


def test_the_brief_is_split_into_the_paragraphs_the_model_wrote() -> None:
    """Blank lines separate the note's parts; the split happens at render.

    A note written before the prompt asked for three paragraphs has no blank
    line in it and stays one paragraph, which is what it is.
    """
    assert split_paragraphs("bir\n\niki\n \nuc") == ["bir", "iki", "uc"]
    assert split_paragraphs("tek paragraf") == ["tek paragraf"]
    assert split_paragraphs(None) == []
    assert split_paragraphs("  ") == []


async def test_the_digest_draws_a_three_paragraph_brief(
    client: TestClient, session: AsyncSession, digest: Run
) -> None:
    digest.editor_note = "Birinci.\n\nIkinci.\n\nUcuncu."
    await session.commit()
    body = client.get("/").text
    assert "<p>Birinci.</p><p>Ikinci.</p><p>Ucuncu.</p>" in body


# -- the topic filter ---------------------------------------------------------


def test_a_short_topic_list_needs_no_disclosure(client: TestClient, digest: Run) -> None:
    """Three tags is a filter. The disclosure only earns its place past six."""
    assert '<details class="more"' not in client.get("/").text


async def test_a_long_topic_list_folds_after_six(
    client: TestClient, session: AsyncSession, digest: Run
) -> None:
    """Thirteen tags laid out at once is a second list to read before the list."""
    summary = (await session.execute(select(Summary).limit(1))).scalars().one()
    summary.tags_json = json.dumps([f"topic{n}" for n in range(9)])
    await session.commit()

    body = client.get("/").text
    assert '<details class="more"' in body
    head, folded = body.split('<details class="more"', 1)
    assert head.count("&amp;tag=") == 6, "six topics before the fold"
    assert "topic8" in folded, "and the rest are still reachable"
