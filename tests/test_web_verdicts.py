"""The reader's verdict (PLAN-EVALS E2.4): one click, survives a reload, counted later."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ainews.config import Settings
from ainews.db import Article, Run, Source, Summary, Verdict
from ainews.web.app import create_app


@pytest.fixture
async def summaries(session: AsyncSession) -> list[int]:
    """One ranked Turkish digest with two stories; returns the summary ids."""
    src = Source(name="OpenAI", url="https://openai.com/news/rss.xml", weight=2.0)
    session.add(src)
    await session.flush()
    run = Run(
        kind="digest", language="tr", status="ok", n_summarized=2, finished_at=datetime.now(UTC)
    )
    session.add(run)
    await session.flush()
    ids = []
    for i in range(2):
        art = Article(
            source_id=src.id,
            title=f"Story {i}",
            url=f"https://openai.com/news/{i}",
            url_canonical=f"https://openai.com/news/{i}",
        )
        session.add(art)
        await session.flush()
        summary = Summary(
            article_id=art.id,
            run_id=run.id,
            language="tr",
            title_local=f"Haber {i}",
            summary="Bir. Iki. Uc.",
            why_it_matters="Onemli.",
            tags_json=json.dumps(["openai"]),
            importance=4,
            rank=i + 1,
        )
        session.add(summary)
        await session.flush()
        ids.append(summary.id)
    await session.commit()
    return ids


@pytest.fixture
def client(settings: Settings, engine: AsyncEngine) -> TestClient:
    return TestClient(create_app(settings))


async def _rows(session: AsyncSession) -> list[Verdict]:
    return list((await session.execute(select(Verdict).order_by(Verdict.id))).scalars())


def test_the_digest_offers_a_verdict_on_every_story(
    client: TestClient, summaries: list[int]
) -> None:
    body = client.get("/").text
    assert body.count('class="vd"') == 2
    assert body.count('hx-post="/verdict?lang=tr"') == 4, "two words per story"
    assert 'aria-pressed="true"' not in body, "nothing is chosen until the reader chooses"
    # Written in both states, so the word reads as a toggle before it is pressed.
    assert body.count('aria-pressed="false"') == 4
    assert "Doğru" in body and "Yanlış" in body


async def test_posting_creates_a_row_and_answers_with_the_fragment_not_a_redirect(
    client: TestClient, summaries: list[int], session: AsyncSession
) -> None:
    response = client.post("/verdict", data={"summary_id": summaries[0], "verdict": "ok"})
    assert response.status_code == 200
    assert response.headers.get("location") is None
    assert f'id="foot-{summaries[0]}"' in response.text
    assert 'aria-pressed="true"' in response.text

    rows = await _rows(session)
    assert [(r.summary_id, r.verdict, r.note) for r in rows] == [(summaries[0], "ok", None)]


async def test_posting_twice_overwrites_rather_than_accumulates(
    client: TestClient, summaries: list[int], session: AsyncSession
) -> None:
    client.post("/verdict", data={"summary_id": summaries[0], "verdict": "ok"})
    client.post(
        "/verdict", data={"summary_id": summaries[0], "verdict": "wrong", "note": "500 bin yok"}
    )
    rows = await _rows(session)
    assert len(rows) == 1
    assert (rows[0].verdict, rows[0].note) == ("wrong", "500 bin yok")


async def test_a_note_is_kept_only_with_wrong(
    client: TestClient, summaries: list[int], session: AsyncSession
) -> None:
    client.post("/verdict", data={"summary_id": summaries[0], "verdict": "wrong", "note": "x"})
    client.post("/verdict", data={"summary_id": summaries[0], "verdict": "ok", "note": "stale"})
    rows = await _rows(session)
    assert (rows[0].verdict, rows[0].note) == ("ok", None)


async def test_pressing_wrong_again_does_not_erase_the_reason(
    client: TestClient, summaries: list[int], session: AsyncSession
) -> None:
    """The two words carry no `note` field; only the note row does.

    A reader who marked a story wrong, wrote why, and then pressed the word a
    second time was silently losing the sentence E5 rewrites the judge prompt
    from. An empty note row still clears it - that is the reader asking.
    """
    client.post(
        "/verdict", data={"summary_id": summaries[0], "verdict": "wrong", "note": "uydurma"}
    )
    client.post("/verdict", data={"summary_id": summaries[0], "verdict": "wrong"})
    rows = await _rows(session)
    assert (rows[0].verdict, rows[0].note) == ("wrong", "uydurma")

    client.post("/verdict", data={"summary_id": summaries[0], "verdict": "wrong", "note": "  "})
    session.expire_all()  # the row above is in the identity map; re-read it
    rows = await _rows(session)
    assert rows[0].note is None


def test_the_page_renders_the_saved_state_after_a_reload(
    client: TestClient, summaries: list[int]
) -> None:
    client.post(
        "/verdict", data={"summary_id": summaries[1], "verdict": "wrong", "note": "tarih uydurma"}
    )
    body = client.get("/").text
    assert body.count('aria-pressed="true"') == 1
    assert 'value="tarih uydurma"' in body, "the reason comes back into its field"
    assert body.count('class="vd-note"') == 1, "only the wrong one grows a note row"


def test_a_story_the_reader_has_judged_keeps_its_words_at_rest(
    client: TestClient, summaries: list[int]
) -> None:
    """V3 hides the control until the pointer arrives - but never a saved mark.

    The reveal is CSS, so the only half of it a test can reach is the class the
    server writes. Without it a reader who marked a story wrong would come back
    to a page that shows no sign of it until he happens to hover the same card
    again, which is the reader's own record hidden from him.
    """
    client.post("/verdict", data={"summary_id": summaries[0], "verdict": "wrong"})
    body = client.get("/").text
    assert body.count('class="vd vd--on"') == 1
    assert body.count('class="vd"') == 1, "the unjudged story stays hover-only"


def test_the_fragment_swapped_in_after_a_press_carries_the_mark(
    client: TestClient, summaries: list[int]
) -> None:
    """HTMX replaces the foot, not the page: the class has to come back with it."""
    text = client.post("/verdict", data={"summary_id": summaries[0], "verdict": "ok"}).text
    assert 'class="vd vd--on"' in text


def test_wrong_opens_the_note_row_and_ok_closes_it(
    client: TestClient, summaries: list[int]
) -> None:
    wrong = client.post("/verdict", data={"summary_id": summaries[0], "verdict": "wrong"}).text
    assert 'class="vd-note"' in wrong
    assert "Kaydedildi" not in wrong, "nothing was saved in the note yet"
    ok = client.post("/verdict", data={"summary_id": summaries[0], "verdict": "ok"}).text
    assert 'class="vd-note"' not in ok


def test_saving_a_note_says_so(client: TestClient, summaries: list[int]) -> None:
    text = client.post(
        "/verdict", data={"summary_id": summaries[0], "verdict": "wrong", "note": "isim yanlış"}
    ).text
    assert "Kaydedildi" in text


def test_a_verdict_on_a_summary_that_does_not_exist_is_a_404(
    client: TestClient, summaries: list[int]
) -> None:
    assert client.post("/verdict", data={"summary_id": 9999, "verdict": "ok"}).status_code == 404


def test_a_verdict_that_is_neither_word_is_refused(
    client: TestClient, summaries: list[int]
) -> None:
    response = client.post("/verdict", data={"summary_id": summaries[0], "verdict": "meh"})
    assert response.status_code == 422


def test_the_fragment_follows_the_interface_language(
    client: TestClient, summaries: list[int]
) -> None:
    text = client.post(
        "/verdict?lang=en", data={"summary_id": summaries[0], "verdict": "wrong"}
    ).text
    assert "Wrong" in text and "What did it get wrong?" in text


# -- the counter on /runs (U1) ------------------------------------------------


def test_the_runs_page_says_how_many_summaries_carry_a_verdict(
    client: TestClient, summaries: list[int]
) -> None:
    """The number is the point of the step, so the page has to state it before
    anything is labelled: "0 / 2" is the sentence that says the count exists."""
    body = client.get("/runs?lang=tr").text

    assert "Karar verilen" in body
    assert "0<small> / 2</small>" in body

    client.post("/verdict", data={"summary_id": summaries[0], "verdict": "ok"})
    assert "1<small> / 2</small>" in client.get("/runs?lang=tr").text


def test_the_counter_states_a_number_and_nothing_else(
    client: TestClient, summaries: list[int]
) -> None:
    """No target, no bar, no reminder. The calibration half of PLAN-EVALS is
    parked behind this count; that is the tool's problem and not a debt the
    reader owes it, and this test is what keeps the next pass from adding one."""
    body = client.get("/runs?lang=en").text

    assert "With a verdict" in body
    assert "100" not in body.split("With a verdict", 1)[1][:400]
    assert "progress" not in body


async def test_a_database_with_no_summaries_still_renders_the_counter(
    client: TestClient, session: AsyncSession
) -> None:
    """`verdict_progress` counts and never divides, so an empty database is a
    row of zeroes rather than the page that would have had to guard a fraction."""
    from ainews.web import queries

    progress = await queries.verdict_progress(session)
    assert (progress.labelled, progress.wrong, progress.total, progress.ok) == (0, 0, 0, 0)
    assert "0<small> / 0</small>" in client.get("/runs?lang=en").text


async def test_the_counter_tells_the_two_labels_apart(
    client: TestClient, summaries: list[int], session: AsyncSession
) -> None:
    """`wrong` is the class the judge's TPR has no denominator without, so it is
    counted separately even though V1 draws only the total (PLAN-EVALS E5)."""
    client.post("/verdict", data={"summary_id": summaries[0], "verdict": "ok"})
    client.post("/verdict", data={"summary_id": summaries[1], "verdict": "wrong", "note": "yok"})

    from ainews.web import queries

    progress = await queries.verdict_progress(session)
    assert (progress.labelled, progress.wrong, progress.ok) == (2, 1, 1)


# ---------------------------------------------------------------------------
# The labels, read back (`/runs/verdicts`)
#
# The note had been write-only since E2: stored on the press, and read by
# exactly one thing - the input it was typed into. E5's human step ("rewrite
# the judge prompt from the `wrong` notes") had no screen. These cover the page
# that gives it one.


def test_the_labels_page_lists_a_verdict_with_its_note(
    client: TestClient, summaries: list[int]
) -> None:
    client.post(
        "/verdict",
        data={"summary_id": summaries[0], "verdict": "wrong", "note": "Tarih uydurma."},
    )
    body = client.get("/runs/verdicts").text
    assert "Tarih uydurma." in body, "the reason is readable without opening the story"
    assert "Haber 0" in body
    assert "OpenAI" in body


def test_the_labels_page_lists_both_words(client: TestClient, summaries: list[int]) -> None:
    """Not only the `wrong` ones: the count this page hangs off says 2/2, and a
    list of one row would be a second number disagreeing with the first."""
    client.post("/verdict", data={"summary_id": summaries[0], "verdict": "ok"})
    client.post("/verdict", data={"summary_id": summaries[1], "verdict": "wrong", "note": "yok"})
    body = client.get("/runs/verdicts").text
    assert body.count('class="verdict') == 2
    assert body.count('class="verdict bad"') == 1, "only the wrong one takes ink"
    assert "—" in body, "a verdict with no reason says so rather than leaving a hole"


def test_the_newest_press_is_first(client: TestClient, summaries: list[int]) -> None:
    """The first column is when the verdict was given, so the sort is that and
    not "wrong first" - a hidden rule would make the times read as unsorted."""
    client.post("/verdict", data={"summary_id": summaries[0], "verdict": "ok"})
    client.post("/verdict", data={"summary_id": summaries[1], "verdict": "wrong"})
    body = client.get("/runs/verdicts").text
    assert body.index("Haber 1") < body.index("Haber 0")


def test_a_label_links_to_the_story_it_was_given_on(
    client: TestClient, summaries: list[int]
) -> None:
    client.post("/verdict", data={"summary_id": summaries[0], "verdict": "wrong", "note": "x"})
    body = client.get("/runs/verdicts").text
    assert f"#story-{summaries[0]}" in body
    assert "all=1" in body, "a verdict can sit on a story below the fold"


async def test_the_link_lands_on_the_story_even_when_it_was_below_the_fold(
    client: TestClient, session: AsyncSession
) -> None:
    """The reason `all=1` is on that href.

    The digest offers the two words on every rendered item, ranked or not, so a
    reader pressing the wrong word on an unranked story creates a label whose story
    the archive does not draw by default. Without the flag the link is a dead
    anchor - a page that loads and goes nowhere, which is worse than an error.
    """
    src = Source(name="OpenAI", url="https://openai.com/news/rss.xml", weight=2.0)
    session.add(src)
    await session.flush()
    run = Run(
        kind="digest", language="tr", status="ok", n_summarized=1, finished_at=datetime.now(UTC)
    )
    session.add(run)
    await session.flush()
    art = Article(
        source_id=src.id,
        title="Below",
        url="https://openai.com/news/below",
        url_canonical="https://openai.com/news/below",
    )
    session.add(art)
    await session.flush()
    summary = Summary(
        article_id=art.id,
        run_id=run.id,
        language="tr",
        title_local="Katlanin altinda",
        summary="Bir. Iki. Uc.",
        why_it_matters="Onemli.",
        tags_json=json.dumps(["openai"]),
        importance=2,
        rank=None,  # never made the top N
    )
    session.add(summary)
    await session.commit()

    client.post("/verdict", data={"summary_id": summary.id, "verdict": "wrong", "note": "n"})
    assert f'id="story-{summary.id}"' not in client.get(f"/archive?run={run.id}").text
    assert f'id="story-{summary.id}"' in client.get(f"/archive?run={run.id}&all=1").text


def test_the_labels_page_says_so_when_nothing_has_been_judged(
    client: TestClient, summaries: list[int]
) -> None:
    body = client.get("/runs/verdicts?lang=en").text
    assert "No story carries a verdict yet" in body
    assert "<table" not in body, "no column headings over an empty list"


def test_the_runs_page_opens_the_labels_from_the_count(
    client: TestClient, summaries: list[int]
) -> None:
    """The way in is the figure itself - the page behind it is what the figure
    is made of - so there is no sixth link in the rail for it."""
    body = client.get("/runs").text
    assert 'href="/runs/verdicts?lang=tr"' in body


def test_the_labels_route_is_not_swallowed_by_the_run_detail_route(
    client: TestClient, summaries: list[int]
) -> None:
    """`/runs/{run_id}` matches any segment and is declared last for this reason;
    a re-ordering would turn this page into "there is no such run"."""
    response = client.get("/runs/verdicts")
    assert response.status_code == 200
    assert "Böyle bir çalışma yok" not in response.text


# ---------------------------------------------------------------------------
# The judge's findings, in front of the reader (`/runs/verdicts`, 2026-09-08)
#
# The sentence the judge could not support was printed once at a terminal and
# filed in `docs/evals.md`; the reader whose verdict calibrates the judge never
# saw it. Each answer here is one label that measures the judge's precision.


async def _judged(session: AsyncSession, summary_id: int, passed: bool, claim: str | None) -> None:
    from ainews.db import EvalResult

    summary = await session.get(Summary, summary_id)
    assert summary is not None
    session.add(
        EvalResult(
            run_id=summary.run_id,
            summary_id=summary_id,
            kind="grounding",
            passed=passed,
            detail=claim,
            model="gpt-5.6-terra",
        )
    )
    await session.commit()


async def test_a_failed_judgement_is_listed_with_its_sentence_and_the_two_words(
    client: TestClient, summaries: list[int], session: AsyncSession
) -> None:
    await _judged(session, summaries[0], False, "500 bin veri kümesi")
    await _judged(session, summaries[1], True, None)
    body = client.get("/runs/verdicts?lang=tr").text
    assert "500 bin veri kümesi" in body
    assert "1 bulgu · 0 tanesine karar verildi" in body
    assert f'id="vd-{summaries[0]}"' in body, "the words sit in the row"
    assert '"frag": "words"' in body, "and swap themselves, not a story foot"
    assert body.count('id="vd-') == 1, "a passed summary is not a finding"


async def test_only_the_latest_judgement_of_a_summary_counts(
    client: TestClient, summaries: list[int], session: AsyncSession
) -> None:
    """Judged twice - say on two tiers - the last word stands."""
    await _judged(session, summaries[0], False, "eski iddia")
    await _judged(session, summaries[0], True, None)
    body = client.get("/runs/verdicts?lang=tr").text
    assert "eski iddia" not in body
    assert "Yargıç henüz bir cümleyi desteksiz bulmadı" in body


async def test_answering_a_finding_swaps_the_words_alone_and_saves_the_label(
    client: TestClient, summaries: list[int], session: AsyncSession
) -> None:
    await _judged(session, summaries[0], False, "uydurma rakam")
    response = client.post(
        "/verdict", data={"summary_id": summaries[0], "verdict": "wrong", "frag": "words"}
    )
    assert response.status_code == 200
    assert response.text.lstrip().startswith("<span"), "the fragment is the control itself"
    assert 'id="foot-' not in response.text, "no story foot inside a table cell"
    assert 'aria-pressed="true"' in response.text

    rows = await _rows(session)
    assert [(r.summary_id, r.verdict) for r in rows] == [(summaries[0], "wrong")]
    assert "1 bulgu · 1 tanesine karar verildi" in client.get("/runs/verdicts?lang=tr").text
