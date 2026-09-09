"""The console shell: the rail, the bar, the story block and the two themes.

Split out of `test_web_pages.py` on 2026-09-08. The shell is what every page
wears (ADR 0021) and its tests are about that one thing - the rail's counts, the
bar's cells, the ink steps of a story card, the theme that must not flash - not
about whether a given URL renders.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import jinja2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ainews.config import Settings
from ainews.db import Bulletin, Run, Summary
from ainews.web.app import create_app
from ainews.web.format import MONTHS, format_stamp, split_paragraphs
from ainews.web.i18n import STRINGS, THEME_COOKIE, strings

# -- the shell ----------------------------------------------------------------


PAGES = ["/", "/search", "/archive", "/sources", "/runs"]


@pytest.mark.parametrize("path", PAGES)
def test_every_page_wears_the_rail(client: TestClient, digest: Bulletin, path: str) -> None:
    """The rail is the only way between five pages, so its absence on any one of
    them is a dead end, not a cosmetic problem."""
    body = client.get(path).text
    assert '<aside class="rail"' in body
    for target in PAGES:
        href = "/?lang=" if target == "/" else f'href="{target}?lang='
        assert href in body, f"{path} does not link to {target}"


@pytest.mark.parametrize("path", PAGES)
def test_the_rail_marks_the_page_you_are_on(
    client: TestClient, digest: Bulletin, path: str
) -> None:
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


def test_the_rail_counts_what_is_behind_each_link(client: TestClient, digest: Bulletin) -> None:
    """The badge counts the stories the link leads to, and it is a COUNT.

    Not `min(run.n_summarized, digest_top_n)`, which is a ceiling the ranker is
    asked to respect rather than a promise it made. The seeded run summarises
    five and ranks three, so that arithmetic says five over a page of three, and
    on a real run it said fifteen over a page of eleven.
    """
    body = client.get("/").text
    assert '<span class="pill pill--acc">3</span>' in body, "three ranked stories today"
    assert '<span class="pill">1</span>' in body, "one digest run in the archive"
    assert body.count('class="item ') == 3, "and the badge is the length of the list"
    assert "3 haber · " in body, "the brief's footnote is the same number"


def test_the_rail_carries_the_advice_not_a_clock(client: TestClient, digest: Bulletin) -> None:
    """Nothing fires on a schedule any more (ADR 0015), so the rail's foot says
    when a run would be worth starting. The seeded digest is minutes old and the
    suggested gap is a day, so the foot counts down rather than saying "now"."""
    body = client.get("/").text
    assert "Önerilen çalıştırma" in body
    assert "sonra</span>" in body, "a countdown, not the scheduler's next fire time"


def test_the_run_page_leads_with_the_advice(client: TestClient, digest: Bulletin) -> None:
    """The block that replaced the cron: a state, a reason, and three facts."""
    body = client.get("/runs").text
    assert 'data-state="waiting"' in body
    assert "Bugünün bülteni alındı" in body, "the state line"
    assert "Önerilen zaman" in body and "Kaynak taraması" in body, "two of the three facts"
    assert 'hx-get="/runs/countdown' in body, "and a countdown that refreshes itself"


def test_the_countdown_is_one_implementation(client: TestClient, digest: Bulletin) -> None:
    """`format_gap` walked the branches in Python and a `setInterval` walked them
    again in JavaScript, on the one line of the page that exists to be trusted.
    The fragment re-fetches itself now and there is no second copy to drift."""
    page = client.get("/runs").text
    assert "setInterval" not in page
    assert "data-u-min=" not in page, "the units were only there for the script"

    fragment = client.get("/runs/countdown?lang=tr").text
    assert 'id="countdown"' in fragment
    assert "sonra" in fragment or "geçti" in fragment
    assert "<html" not in fragment, "a fragment, not a page"


async def test_a_stale_digest_turns_the_block_into_an_invitation(
    client: TestClient, session: AsyncSession, digest: Bulletin
) -> None:
    """The state a reader meets on any morning after the first.

    The seeded digest is minutes old, so it is aged past the suggested gap here
    rather than in a second fixture - what changes on the page is the sentence
    and the countdown's sign, and nothing else moves.

    The advice is anchored on the last successful *press*, not on the bulletin
    it published, so the run behind the fixture is what moves.
    """
    run = await session.get(Run, digest.run_id)
    assert run is not None
    run.started_at = datetime.now(UTC) - timedelta(hours=30)
    run.finished_at = run.started_at + timedelta(minutes=1)
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
def test_only_the_run_page_can_start_a_run(path: str, client: TestClient, digest: Bulletin) -> None:
    """The press is on `/runs` and nowhere else.

    What every other page carries is the way in - the runs link, drawn in the
    accent - and nothing that spends money. In the top bar, or in the rail's
    foot, the one control that costs money is drawn on every page at the size of
    a preference.
    """
    body = client.get(path).text
    assert "/runs/start" not in body and "/runs/confirm" not in body
    assert body.count('class="nav--go"') == 1, "the accented link is in the foot"
    assert body.count('class="nav--narrow"') == 1, "and once more for the phone"


def test_the_press_asks_before_it_spends(client: TestClient, digest: Bulletin) -> None:
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
    client: TestClient, digest: Bulletin
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
    from ainews.pipeline import runner

    settings.openai_api_key = "sk-test"
    produced: list[str] = []

    async def _record(language: str | None = None, **_: object) -> str:
        produced.append(language or "")
        return ""

    monkeypatch.setattr(runner, "run_digest", _record)

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


def test_search_finds_a_story_whatever_the_shell_is_set_to(
    client: TestClient, digest: Bulletin
) -> None:
    """The index is not filtered by the page's language either (ADR 0017).

    A reader looking for a company name wants the article; refusing it because
    the bulletin covering it was written in the other language is the same
    content filter the switch stopped being.
    """
    body = client.get("/search?q=OpenAI&lang=en").text
    assert "OpenAI ucuz bir model duyurdu" in body


def test_the_archive_lists_every_bulletin_and_names_the_odd_ones_out(
    client: TestClient, digest: Bulletin
) -> None:
    body = client.get("/archive?lang=en").text
    assert "Gunun ortak konusu" in body, "a Turkish bulletin is still in the archive"
    assert '<span lang="tr">Türkçe</span>' in body, "and its pill says which language it is"


# -- the theme ----------------------------------------------------------------


def test_the_default_theme_writes_no_attribute(client: TestClient, digest: Bulletin) -> None:
    """No `data-theme` is the follow-the-system state: `color-scheme: light dark`
    does the work, and an attribute would override the reader's own setting."""
    body = client.get("/").text
    assert "data-theme" not in body
    assert client.cookies.get(THEME_COOKIE) == "system"


@pytest.mark.parametrize("value", ["light", "dark"])
def test_a_chosen_theme_is_rendered_and_remembered(
    client: TestClient, digest: Bulletin, value: str
) -> None:
    response = client.get(f"/?theme={value}")
    assert f'data-theme="{value}"' in response.text
    assert response.cookies.get(THEME_COOKIE) == value
    # The cookie alone has to carry it to the next page, with no query string.
    assert f'data-theme="{value}"' in client.get("/runs").text


def test_a_nonsense_theme_falls_back_to_the_system(client: TestClient, digest: Bulletin) -> None:
    assert "data-theme" not in client.get("/?theme=neon").text


def test_the_theme_links_keep_the_rest_of_the_query(client: TestClient, digest: Bulletin) -> None:
    body = client.get("/?all=1&tag=policy").text
    assert "all=1" in body and "tag=policy" in body and "theme=dark" in body


# -- the story block ----------------------------------------------------------


def test_a_story_names_its_impact_band(client: TestClient, digest: Bulletin) -> None:
    """The bars are a shape; the word beside them is what makes it a scale."""
    body = client.get("/").text
    assert "Yüksek etki" in body, "the 5 and the 4 are the high band"
    assert "Orta etki" in body, "the 3 is the middle one"


def test_the_impact_meter_lights_one_bar_per_step_of_the_band(
    client: TestClient, digest: Bulletin
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


def test_why_it_matters_is_labelled_and_separate(client: TestClient, digest: Bulletin) -> None:
    """It is the one thing here an RSS reader does not have, so it is not left
    as bold text inside the summary."""
    body = client.get("/").text
    assert "Neden önemli" in body
    assert '<div class="why">' in body


def test_a_story_carries_its_topics_and_they_filter(client: TestClient, digest: Bulletin) -> None:
    body = client.get("/").text
    assert 'class="chip" href="/?lang=tr&amp;all=1&amp;tag=openai"' in body
    # And following one narrows the list, which is the only reason it is a link.
    assert client.get("/?all=1&tag=openai").text.count('class="item ') == 2


def test_a_source_keeps_its_own_capitals(client: TestClient, digest: Bulletin) -> None:
    """It was lowercased to `openai`, which is the one word on the meta row a
    reader recognises at a glance."""
    body = client.get("/").text
    assert "</span>OpenAI" in body
    assert "</span>openai" not in body


def test_every_story_offers_its_source(client: TestClient, digest: Bulletin) -> None:
    body = client.get("/").text
    assert body.count('class="go"') == 3, "one per story above the fold"
    assert "Kaynağa git" in body


def test_a_low_scoring_story_is_only_a_headline(client: TestClient, digest: Bulletin) -> None:
    """Ranks 2 and 1 collapse in CSS, so the markup has to stay identical -
    the test is that they are still whole items, not truncated ones."""
    body = client.get("/?all=1").text
    assert body.count('class="item ') == 5
    assert body.count('<div class="why">') == 5


# -- the header and the rail --------------------------------------------------


def test_the_reading_page_says_nothing_about_the_machine(
    client: TestClient, digest: Bulletin
) -> None:
    """Cost is not deleted, it is placed - and the place is `/runs`.

    It came off the top bar on 2026-09-05 and off the digest's side column on
    2026-09-06, on the same argument both times: it says how the page was made,
    not what is on it. The last line about the machine went with the right rail
    on 2026-09-08 - the run log states it at the head of the page and the left
    rail's foot states it on every page, so the reading holds no copy at all.
    """
    body = client.get("/").text
    assert "$0.042" not in body, "spend belongs to the run log"
    assert "$0.042" in client.get("/runs").text
    assert 'class="status"' not in body, "and so does the machine's own state"


def test_the_digest_reads_the_note_then_the_topics_then_the_day(
    client: TestClient, digest: Bulletin
) -> None:
    """The order is the page's argument, and it survived the right rail.

    The brief and the impact spread were units of a column beside the feed; ADR
    0024 took the column off and they kept the order they already had, above the
    stories rather than beside them.
    """
    body = client.get("/").text
    order = [
        body.index(">Bugünün özeti<"),
        body.index('<div class="filter">'),
        body.index('class="spread"'),
        body.index('<ol class="feed">'),
    ]
    assert order == sorted(order)
    assert 'class="side"' not in body, "there is no column beside the reading"


def test_every_count_is_a_count_of_the_list_you_can_reach(
    client: TestClient, digest: Bulletin
) -> None:
    """The filter pill and the feed have to agree.

    Counting the tags over every summary the run produced while the filter
    narrows the ranked ones, a pill promises 27 stories on a page holding
    fifteen and returns four when pressed. There were three parties to this
    agreement until the right rail took the themes panel with it (ADR 0024).

    Seeded: five summaries, three of them ranked. `openai` is on the first two
    (both ranked), `policy` on the last three (one ranked).
    """
    body = client.get("/").text
    assert "openai <b>2</b>" in body and "policy <b>1</b>" in body
    # And the promise holds when pressed.
    assert client.get("/?tag=openai").text.count('class="item ') == 2
    assert client.get("/?tag=policy").text.count('class="item ') == 1


def test_showing_everything_widens_the_counts_with_the_list(
    client: TestClient, digest: Bulletin
) -> None:
    """`all=1` is the same agreement over a longer list, not a different rule."""
    body = client.get("/?all=1").text
    assert "openai <b>2</b>" in body and "policy <b>3</b>" in body
    assert body.count('class="item ') == 5


def test_the_briefs_footnote_counts_topics_rather_than_reporting_a_cap(
    client: TestClient, digest: Bulletin
) -> None:
    """The filter row draws at most twelve topics; the footnote counts all of
    them. It used to count the drawn ones, so on any real day it printed the
    cap - `12 konu` - as though it were a measurement."""
    # The seeded ranked three carry `openai`, `models` and `policy`.
    assert "3 haber · 1 kaynak · 3 konu" in client.get("/").text
    # And it stays the size of the digest when the expander widens the list:
    # the two unranked stories carry `policy`, which is already counted.
    assert "3 haber · 1 kaynak · 3 konu" in client.get("/?all=1").text


def test_the_impact_spread_keeps_all_three_bands(client: TestClient, digest: Bulletin) -> None:
    """Three ranked stories: one 5, one 4, one 3 - so two high, one mid, no low.
    The empty band keeps its row, dimmed, because a scale with holes in it is
    still read as a scale.

    It is drawn at the end of the topic row (ADR 0024), so what it is read
    against is the row three lines above the stories rather than a column
    standing beside them.
    """
    spread = client.get("/").text.split('class="spread"', 1)[1].split("</ul>", 1)[0]
    assert spread.count('<li class="score--') == 3
    assert 'class="score--low is-off"' in spread


def test_the_spread_counts_the_list_the_reader_can_reach(
    client: TestClient, digest: Bulletin
) -> None:
    """It is counted off the stories in view rather than off the run, so a
    filter narrows it along with the feed. Seeded: three ranked (5, 4, 3), and
    `openai` is on the two that scored highest."""

    def counts(path: str) -> list[str]:
        page = client.get(path).text
        spread = page.split('class="spread"', 1)[1].split("</ul>", 1)[0]
        return re.findall(r'<span class="spread__n">(\d+)</span>', spread)

    assert counts("/") == ["2", "1", "0"]
    assert counts("/?tag=openai") == ["2", "0", "0"]


def test_the_week_is_read_from_real_runs_on_the_run_log(
    client: TestClient, digest: Bulletin
) -> None:
    """Nothing in it is projected or filled in - if the numbers were invented
    the chart would not be worth the space it takes.

    It is on `/runs` rather than beside the reading (ADR 0024): it is the
    record's own subject read at a different grain, and the reading page has
    nowhere to put a chart.
    """
    body = client.get("/runs").text
    assert 'class="chart"' in body
    assert body.count('class="chart__b"') == 7, "seven local days"
    # The seeded run summarised five stories today, so today's bar is the tallest.
    assert 'style="height: 100%"' in body
    assert '<li class="on"' in body
    assert 'class="chart"' not in client.get("/").text, "and not on the reading"


def test_no_story_is_dressed_differently_for_being_first(
    client: TestClient, digest: Bulletin
) -> None:
    """Size on this page means importance and nothing else. The first story used
    to carry a `data-lead` marker and a plate; two ranking systems in one column
    left the reader unable to tell size-by-score from size-by-position, so the
    marker is gone from every list (ADR 0014)."""
    for path in ("/", "/archive", "/search?q=Ozet"):
        assert "data-lead" not in client.get(path).text, path


async def test_the_week_survives_an_empty_database(
    client: TestClient, session: AsyncSession
) -> None:
    """The emptiest page is exactly when a reader wants to know whether anything
    has ever run, so `/runs` draws its seven days with no runs behind them - a
    flat chart rather than a missing one."""
    body = client.get("/runs").text
    assert "Henüz çalışma yok" in body
    assert 'class="chart"' in body
    assert 'style="height: 0%"' in body


def test_the_rail_groups_its_five_links(client: TestClient, digest: Bulletin) -> None:
    body = client.get("/").text
    for label in ("Günlük", "Keşfet", "Kayıt"):
        assert f'<p class="nav__g">{label}</p>' in body


def test_the_keyboard_can_skip_the_shell(client: TestClient, digest: Bulletin) -> None:
    body = client.get("/").text
    assert '<a class="skip" href="#main">' in body
    assert 'id="main"' in body


def test_the_language_switch_draws_both_languages_and_marks_the_current_one(
    client: TestClient, digest: Bulletin
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
    client: TestClient, digest: Bulletin
) -> None:
    body = client.get("/?lang=en").text
    assert '<a href="/?lang=en" lang="en" hreflang="en" aria-current="true">' in body


def test_the_stamp_names_the_month_in_the_pages_language() -> None:
    """A Turkish page used to read "04 sep": the C locale's month, lower-cased."""
    moment = datetime(2026, 9, 4, 11, 18, tzinfo=UTC)
    assert "Eyl" in format_stamp(moment, "tr")
    assert "Sep" in format_stamp(moment, "en")
    assert format_stamp(None, "tr") == "—"


def test_the_bar_stamp_follows_the_language(client: TestClient, digest: Bulletin) -> None:
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
    client: TestClient, session: AsyncSession, digest: Bulletin
) -> None:
    digest.editor_note = "Birinci.\n\nIkinci.\n\nUcuncu."
    await session.commit()
    body = client.get("/").text
    assert "<p>Birinci.</p><p>Ikinci.</p><p>Ucuncu.</p>" in body


# -- the topic filter ---------------------------------------------------------


def test_a_short_topic_list_needs_no_disclosure(client: TestClient, digest: Bulletin) -> None:
    """Three tags is a filter. The disclosure only earns its place past six."""
    assert '<details class="tag-more"' not in client.get("/").text


async def test_a_long_topic_list_folds_after_six(
    client: TestClient, session: AsyncSession, digest: Bulletin
) -> None:
    """Thirteen tags laid out at once is a second list to read before the list."""
    summary = (await session.execute(select(Summary).limit(1))).scalars().one()
    summary.tags_json = json.dumps([f"topic{n}" for n in range(9)])
    await session.commit()

    body = client.get("/").text
    assert '<details class="tag-more"' in body
    head, folded = body.split('<details class="tag-more"', 1)
    assert head.count("&amp;tag=") == 6, "six topics before the fold"
    assert "topic8" in folded, "and the rest are still reachable"


# -- the shell's top bar -------------------------------------------------------


def test_the_bar_spans_the_shell_and_carries_the_brand(
    client: TestClient, digest: Bulletin
) -> None:
    """One bar across the window, with the rail hanging under its left cell.

    As the first child of the content column, the bar makes the window's top
    edge two bands: the brand in the rail's own box, and the reader's controls
    starting 264px in. The order in the markup is what says
    which of the two shells is being drawn - the bar before the rail, both
    inside `.app` - so this asserts the order rather than a class name.
    """
    body = client.get("/").text
    app = body.split('<div class="app', 1)[1]
    bar = app.index('<header class="top-bar"')
    rail = app.index('<aside class="rail"')
    assert bar < rail, "the bar is a row of the shell, above the rail"

    head, rest = app.split('<aside class="rail"', 1)
    assert 'class="bar__brand"' in head, "the brand is the bar's first cell"
    assert 'class="brand"' not in rest.split("</aside>", 1)[0], "and not the rail's head"


def test_the_shell_is_one_rail_and_the_reading(client: TestClient, digest: Bulletin) -> None:
    """The right rail is not needed any more.

    It was a card inside the digest's content block from 2026-09-06, then a
    column of the shell from 2026-09-07 (ADR 0021, on his note that a rail
    cannot be a separate structure), and then nothing. Every page is two columns
    now, so `.app` carries no modifier at all and no page has a third cell.
    """
    for path in ("/", "/runs", "/sources", "/search", "/archive"):
        body = client.get(path).text
        assert '<div class="app">' in body, path
        assert "app--side" not in body, path
        assert 'class="side"' not in body, path


def test_the_rail_can_be_put_away_and_says_so(client: TestClient, digest: Bulletin) -> None:
    """The collapse is a button, wired to the rail it acts on.

    `aria-controls` and `aria-expanded` are the whole of what a screen reader
    gets here: the chevron says nothing out loud, and the rail it collapses is a
    different element from the button that does it.
    """
    body = client.get("/").text
    assert 'id="rail-tog"' in body
    assert 'aria-controls="rail"' in body
    assert '<aside class="rail" id="rail">' in body
    assert 'aria-expanded="true"' in body, "the rail is open until the reader says otherwise"
    assert 'aria-label="Menüyü daralt veya genişlet"' in body


def test_every_rail_link_keeps_a_name_a_pointer_can_find(
    client: TestClient, digest: Bulletin
) -> None:
    """Collapsed, the rail is five icons. An icon has to be learned, so each one
    keeps its label in the accessibility tree and a `title` for the hover.

    The theme switch in the foot is not in this: it keeps its three words drawn
    at every width, which is what a control with no icon has to do.
    """
    rail = client.get("/").text.split('<aside class="rail"', 1)[1].split("</aside>", 1)[0]
    links = [
        line
        for line in rail.splitlines()
        if line.lstrip().startswith("<a ") and "?theme=" not in line
    ]
    # Five destinations, and the run log is written twice - the foot's copy and
    # the one the phone's bottom bar draws.
    assert len(links) == 6
    for link in links:
        assert "title=" in link, f"no hover name on {link.strip()[:60]}"


def test_the_two_switches_are_one_group_at_the_head_of_the_right_rail(
    client: TestClient, digest: Bulletin
) -> None:
    """The theme and the language are the same kind of control - a thing you set
    once - so they share the bar's last cell, which is also the head of the
    right rail. Apart, they sit at opposite corners of the window: the language
    top right, the theme at the foot of the rail (ADR 0013).

    The theme switch is written once as a result. Rendered twice and drawn once
    - because the rail's foot disappears on a phone and the bar carries a spare
    - the pattern costs a real bug: a `display: none`
    placed above the rule it was overriding drew both on every wide window.
    """
    body = client.get("/").text
    assert body.count("seg seg--theme") == 1, "one render, at every width"
    cell = body.split('class="bar__side"', 1)[1].split("</div>", 3)
    assert "seg--lang" in "".join(cell[:3]) and "seg--theme" in "".join(cell[:3])
    rail = body.split('<aside class="rail"', 1)[1].split("</aside>", 1)[0]
    assert "seg--theme" not in rail, "the rail's foot gave it up"


def test_the_bar_spells_the_bulletins_date_out(client: TestClient, digest: Bulletin) -> None:
    """`06 Eyl · 16:06` in the machine face was a log line stuck to the page
    name. It is the one date on the page a person would say out loud."""
    body = client.get("/").text
    assert 'class="bar__date"' in body
    assert str(datetime.now(UTC).year) in body.split('class="bar__date"', 1)[1][:200]


def test_the_why_plate_is_never_the_surface_it_is_inset_into(client: TestClient) -> None:
    """A plate the same colour as the card it sits in is not a plate.

    `--plate` was `#FFFFFF` in light, which was a step up from the page while the
    story sat directly on it. The story got a card back on 2026-09-07 and the
    card is white: the same token became invisible in the one theme where the
    block it draws - the conclusion of every story - has no other mark on it.
    Both themes, both values, checked against the surface underneath.
    """
    css = client.get("/static/theme.css").text

    def token(name: str) -> tuple[str, str]:
        raw = css.split(f"--{name}:", 1)[1].split(";", 1)[0]
        light, dark = raw.split("light-dark(", 1)[1].rsplit(")", 1)[0].split(",")
        return light.strip().lower(), dark.strip().lower()

    plate, panel = token("plate"), token("panel")
    assert plate[0] != panel[0], "the plate vanishes into the card in the light theme"
    assert plate[1] != panel[1], "the plate vanishes into the card in the dark theme"


# -- the two dictionaries ------------------------------------------------------


def test_the_two_dictionaries_hold_the_same_words() -> None:
    """One language gaining a string and the other not is the whole failure mode
    of keeping two dictionaries by hand, and it is invisible on screen: the page
    that is missing the key renders the label blank."""
    assert set(STRINGS["tr"]) == set(STRINGS["en"])


def test_a_missing_interface_string_is_loud() -> None:
    """`i18n.py` opens by saying a missing key is loud rather than a silent
    fallback nobody notices. It was not: Jinja swallows `LookupError` on both
    `t.foo` and `t["foo"]` and renders `Undefined` as the empty string, so a
    dropped key was a blank label in one language with nothing in the log."""
    t = strings("tr")
    template = jinja2.Environment().from_string("[{{ t.nope }}][{{ t['nope'] }}]")

    with pytest.raises(jinja2.UndefinedError, match="nope"):
        template.render(t=t)

    # The lookups that want a quiet miss ask instead of catching, and keep it.
    assert t.get("nope") is None
    assert "nope" not in t
