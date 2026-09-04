from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings
from ainews.db import Article, Source
from ainews.pipeline.nodes.collect import collect_articles
from ainews.sources.rss import fetch_feed, make_client, parse_feed_bytes
from ainews.sources.seed import load_seed_feeds, sync_sources

FEED_URL = "https://example.com/feed.xml"


def _rss(*entries: str) -> bytes:
    items = "".join(entries)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Example</title>{items}</channel></rss>
    """.encode()


def _item(title: str, link: str, *, published: datetime | None = None, body: str = "") -> str:
    date = published or datetime.now(UTC)
    return (
        f"<item><title>{title}</title><link>{link}</link>"
        f"<pubDate>{date.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
        f"<description>{body}</description></item>"
    )


# -- parsing ------------------------------------------------------------------


def test_parse_extracts_title_link_date_and_body() -> None:
    items = parse_feed_bytes(_rss(_item("A model shipped", "https://x.dev/a", body="Body text")))
    assert len(items) == 1
    assert items[0].title == "A model shipped"
    assert items[0].url_canonical == "https://x.dev/a"
    assert items[0].published_at is not None
    assert "Body text" in (items[0].body_text or "")


def test_entries_without_a_link_or_title_are_dropped() -> None:
    payload = _rss(
        "<item><title>No link</title></item>",
        "<item><link>https://x.dev/b</link></item>",
        _item("Good", "https://x.dev/c"),
    )
    assert [i.title for i in parse_feed_bytes(payload)] == ["Good"]


# -- conditional GET ----------------------------------------------------------


@respx.mock
async def test_etag_is_sent_back_and_304_is_not_an_error() -> None:
    route = respx.get(FEED_URL).mock(return_value=httpx.Response(304))
    async with make_client() as client:
        result = await fetch_feed(client, FEED_URL, etag='W/"abc"', modified="Wed, 03 Sep 2026")

    assert route.calls.last.request.headers["If-None-Match"] == 'W/"abc"'
    assert route.calls.last.request.headers["If-Modified-Since"] == "Wed, 03 Sep 2026"
    assert result.ok is True
    assert result.not_modified is True
    assert result.items == []


@respx.mock
async def test_validators_come_back_from_the_response() -> None:
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            content=_rss(_item("T", "https://x.dev/a")),
            headers={"ETag": 'W/"v2"', "Last-Modified": "Thu, 04 Sep 2026 06:00:00 GMT"},
        )
    )
    async with make_client() as client:
        result = await fetch_feed(client, FEED_URL)
    assert result.etag == 'W/"v2"'
    assert result.modified == "Thu, 04 Sep 2026 06:00:00 GMT"


@respx.mock
async def test_a_browser_user_agent_is_sent() -> None:
    """The Verge and Ars Technica reject the default Python agent outright."""
    route = respx.get(FEED_URL).mock(return_value=httpx.Response(304))
    async with make_client() as client:
        await fetch_feed(client, FEED_URL)
    assert "Mozilla/5.0" in route.calls.last.request.headers["user-agent"]


@respx.mock
async def test_transport_errors_are_reported_not_raised() -> None:
    respx.get(FEED_URL).mock(side_effect=httpx.ConnectError("boom"))
    async with make_client() as client:
        result = await fetch_feed(client, FEED_URL)
    assert result.ok is False
    assert "boom" in (result.error or "")


# -- collect ------------------------------------------------------------------


@respx.mock
async def test_collect_inserts_once_and_then_nothing(
    session: AsyncSession, settings: Settings
) -> None:
    payload = _rss(_item("One", "https://x.dev/1"), _item("Two", "https://x.dev/2"))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=payload))
    session.add(Source(name="Example", url=FEED_URL))
    await session.commit()

    first = await collect_articles(session, settings)
    assert (first.n_seen, first.n_new) == (2, 2)

    second = await collect_articles(session, settings)
    assert second.n_new == 0, "the same payload must not create rows twice"

    count = len((await session.execute(select(Article))).scalars().all())
    assert count == 2


@respx.mock
async def test_archive_entries_older_than_the_horizon_are_skipped(
    session: AsyncSession, settings: Settings
) -> None:
    """OpenAI's feed ships 1169 entries; only the current ones are news."""
    old = datetime.now(UTC) - timedelta(days=settings.collect_max_age_days + 3)
    payload = _rss(
        _item("Fresh", "https://x.dev/fresh"),
        _item("Ancient", "https://x.dev/ancient", published=old),
    )
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=payload))
    session.add(Source(name="Example", url=FEED_URL))
    await session.commit()

    stats = await collect_articles(session, settings)
    assert stats.n_new == 1
    titles = [a.title for a in (await session.execute(select(Article))).scalars()]
    assert titles == ["Fresh"]


@respx.mock
async def test_one_failing_feed_does_not_stop_the_others(
    session: AsyncSession, settings: Settings
) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(404))
    respx.get("https://ok.dev/feed").mock(
        return_value=httpx.Response(200, content=_rss(_item("Live", "https://ok.dev/1")))
    )
    session.add_all(
        [Source(name="Dead", url=FEED_URL), Source(name="Live", url="https://ok.dev/feed")]
    )
    await session.commit()

    stats = await collect_articles(session, settings)
    assert stats.n_failed == 1
    assert stats.n_new == 1


@respx.mock
async def test_a_source_disables_itself_after_repeated_failures(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(FEED_URL).mock(return_value=httpx.Response(500))
    session.add(Source(name="Rotting", url=FEED_URL))
    await session.commit()

    for _ in range(settings.source_max_failures):
        await collect_articles(session, settings)

    source = (await session.execute(select(Source))).scalar_one()
    assert source.enabled is False
    assert source.consecutive_failures == settings.source_max_failures
    assert "disabled" in (source.last_status or "") or source.consecutive_failures >= 5


@respx.mock
async def test_syndicated_article_is_stored_once(session: AsyncSession, settings: Settings) -> None:
    """Two feeds, one story, one row - the canonical URL is the key."""
    shared = "https://x.dev/story"
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=_rss(_item("Story", shared))))
    respx.get("https://b.dev/feed").mock(
        return_value=httpx.Response(200, content=_rss(_item("Story", shared + "?utm_source=b")))
    )
    session.add_all([Source(name="A", url=FEED_URL), Source(name="B", url="https://b.dev/feed")])
    await session.commit()

    stats = await collect_articles(session, settings)
    assert stats.n_seen == 2
    assert stats.n_new == 1


# -- seeding ------------------------------------------------------------------


def test_shipped_feed_list_is_well_formed() -> None:
    feeds = load_seed_feeds()
    assert len(feeds) >= 15
    urls = [f["url"] for f in feeds]
    assert len(urls) == len(set(urls)), "duplicate feed URL in feeds.yaml"
    assert all(str(u).startswith("https://") for u in urls)
    assert not any("arxiv" in str(u).lower() for u in urls), "arXiv was ruled out in PLAN.md"


async def test_seeding_is_additive_and_respects_operator_changes(session: AsyncSession) -> None:
    added = await sync_sources(session)
    assert added >= 15

    disabled = (await session.execute(select(Source))).scalars().first()
    assert disabled is not None
    disabled.enabled = False
    await session.commit()

    assert await sync_sources(session) == 0
    await session.refresh(disabled)
    assert disabled.enabled is False, "a re-sync must not re-enable what the operator disabled"
