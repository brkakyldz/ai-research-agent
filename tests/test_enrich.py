from __future__ import annotations

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings, get_settings
from ainews.db import Article, DailyCounter, Source
from ainews.pipeline.nodes.enrich import VERSION_TITLE, enrich_articles
from ainews.sources import tavily
from ainews.sources.extract import MIN_USABLE_CHARS, clean_html, fetch_article, is_usable
from ainews.sources.rss import make_client

LONG = "Bir model duyuruldu ve bu duyuru sektor icin onemli. " * 20


async def _article(
    session: AsyncSession,
    *,
    body: str | None,
    weight: float = 1.5,
    slug: str = "a",
    title: str = "A headline about a model",
) -> Article:
    src = Source(name=f"S{slug}", url=f"https://s{slug}.dev/feed", weight=weight)
    session.add(src)
    await session.flush()
    art = Article(
        source_id=src.id,
        title=title,
        url=f"https://s{slug}.dev/{slug}",
        url_canonical=f"https://s{slug}.dev/{slug}",
        body_text=body,
    )
    session.add(art)
    await session.commit()
    return art


# -- extraction ---------------------------------------------------------------


def test_html_is_reduced_to_text() -> None:
    cleaned = clean_html(f"<div><p>{LONG}</p><script>ignored()</script></div>")
    assert "ignored()" not in cleaned
    assert "<p>" not in cleaned
    assert LONG.split(".")[0] in cleaned


def test_a_short_teaser_survives_even_when_trafilatura_finds_no_article() -> None:
    """A teaser is thin, but it beats an empty body."""
    assert clean_html("<p>Two sentences. That is all there is.</p>").startswith("Two sentences")


def test_empty_and_none_are_handled() -> None:
    assert clean_html(None) == ""
    assert clean_html("   ") == ""


def test_usability_threshold() -> None:
    assert is_usable("x" * MIN_USABLE_CHARS) is True
    assert is_usable("x" * (MIN_USABLE_CHARS - 1)) is False
    assert is_usable(None) is False


@respx.mock
async def test_fetch_returns_empty_string_instead_of_raising() -> None:
    respx.get("https://dead.dev/a").mock(side_effect=httpx.ConnectError("nope"))
    assert await fetch_article("https://dead.dev/a") == ""


@respx.mock
async def test_non_html_responses_are_skipped() -> None:
    respx.get("https://x.dev/a.pdf").mock(
        return_value=httpx.Response(
            200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"}
        )
    )
    assert await fetch_article("https://x.dev/a.pdf") == ""


@respx.mock
async def test_the_body_fetch_shares_the_feed_polls_client() -> None:
    """One HTTP stack. It was a blocking `httpx.get` inside a thread, so the
    project ran two - two pools, two sets of defaults - and held a worker thread
    for the length of a fifteen-second timeout to do nothing but wait."""
    route = respx.get("https://x.dev/a").mock(
        return_value=httpx.Response(
            200,
            content=b"<html><body><article>" + b"word " * 200 + b"</article></body></html>",
            headers={"content-type": "text/html"},
        )
    )
    async with make_client() as client:
        assert await fetch_article("https://x.dev/a", client) != ""
        assert await fetch_article("https://x.dev/a", client) != ""
    assert route.call_count == 2


# -- tiering ------------------------------------------------------------------


@respx.mock
async def test_a_full_feed_body_costs_no_request(session: AsyncSession, settings: Settings) -> None:
    art = await _article(session, body=f"<p>{LONG}</p>")
    route = respx.get(art.url).mock(return_value=httpx.Response(200))

    stats = await enrich_articles(session, [art.id], settings)

    assert stats.n_from_feed == 1
    assert stats.n_fetched == 0
    assert not route.called, "tier 1 must not reach the network"


@respx.mock
async def test_a_thin_feed_body_triggers_a_fetch(session: AsyncSession, settings: Settings) -> None:
    art = await _article(session, body="<p>Teaser only.</p>")
    respx.get(art.url).mock(
        return_value=httpx.Response(
            200,
            html=f"<html><body><article><p>{LONG}</p></article></body></html>",
        )
    )

    stats = await enrich_articles(session, [art.id], settings)
    await session.refresh(art)

    assert stats.n_fetched == 1
    assert len(art.body_text or "") >= MIN_USABLE_CHARS


@respx.mock
async def test_a_failed_fetch_keeps_the_teaser(session: AsyncSession, settings: Settings) -> None:
    art = await _article(session, body="<p>Teaser only.</p>")
    respx.get(art.url).mock(return_value=httpx.Response(403))

    await enrich_articles(session, [art.id], settings)
    await session.refresh(art)

    assert art.body_text is not None
    assert "Teaser only" in art.body_text


# -- the Tavily cap -----------------------------------------------------------


@pytest.mark.parametrize(
    "title", ["llm 0.35", "llm-anthropic 0.28", "llm-openrouter 0.7.1", "Rerun v0.35.2"]
)
def test_a_name_and_a_version_number_is_not_a_news_query(title: str) -> None:
    assert VERSION_TITLE.match(title), title


@pytest.mark.parametrize(
    "title",
    [
        "The complex corporate web behind a $3.2 billion AI data center",
        "Python 3.15 released",
        "GPT-5.5",
        "A headline about a model",
    ],
)
def test_a_version_number_inside_a_sentence_still_qualifies(title: str) -> None:
    assert not VERSION_TITLE.match(title), title


@respx.mock
async def test_a_release_note_title_never_spends_a_credit(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`llm-anthropic 0.28`, searched as news on 2026-09-08, came back as every
    page on the web containing "0.28" and the summariser wrote a headline about
    an Anthropic evaluation that never happened."""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    get_settings.cache_clear()
    art = await _article(session, body=None, title="llm-anthropic 0.28")
    respx.get(art.url).mock(return_value=httpx.Response(404))

    called = False

    def _boom(*args: object, **kwargs: object) -> str:
        nonlocal called
        called = True
        return "the LLM model identified as '0.28' refers to Claude Sonnet 5"

    monkeypatch.setattr(tavily, "_search_sync", _boom)
    stats = await enrich_articles(session, [art.id], get_settings())
    await session.refresh(art)

    assert not called
    assert stats.n_tavily == 0 and stats.n_still_empty == 1
    assert not art.body_text


@respx.mock
async def test_tavily_is_not_called_when_no_key_is_configured(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conftest leaves TAVILY_API_KEY empty; enrichment must degrade quietly."""
    art = await _article(session, body=None)
    respx.get(art.url).mock(return_value=httpx.Response(404))

    called = False

    def _boom(*args: object, **kwargs: object) -> str:
        nonlocal called
        called = True
        return ""

    monkeypatch.setattr(tavily, "_search_sync", _boom)
    stats = await enrich_articles(session, [art.id], settings)

    assert called is False
    assert stats.n_tavily == 0
    assert stats.n_still_empty == 1


@respx.mock
async def test_the_daily_cap_stops_spending(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap is the point of the module: it must hold across calls."""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    monkeypatch.setenv("TAVILY_DAILY_CAP", "2")
    get_settings.cache_clear()
    settings = get_settings()

    calls = 0

    def _fake(query: str, _settings: Settings) -> str:
        nonlocal calls
        calls += 1
        return "enrichment text"

    monkeypatch.setattr(tavily, "_search_sync", _fake)

    for i in range(5):
        assert isinstance(await tavily.enrich(session, f"query {i}", settings), str)

    assert calls == 2, "the third search must not happen"
    assert await tavily.remaining_credits(session, settings) == 0


async def test_the_counter_is_stored_not_held_in_memory(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that restarts twice a day would otherwise reset its own budget."""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(tavily, "_search_sync", lambda *_: "text")

    await tavily.enrich(session, "one", settings)
    row = await tavily.counters_for_day(session)

    assert isinstance(row, DailyCounter)
    assert row.tavily_credits == 1


async def test_a_failing_search_still_costs_its_credit(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tavily bills the request, so a retry loop must not be free."""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    get_settings.cache_clear()
    settings = get_settings()

    def _raise(*_: object) -> str:
        raise RuntimeError("upstream 502")

    monkeypatch.setattr(tavily, "_search_sync", _raise)

    assert await tavily.enrich(session, "q", settings) == ""
    assert await tavily.credits_used_today(session) == 1
