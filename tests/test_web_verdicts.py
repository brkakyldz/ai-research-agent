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
