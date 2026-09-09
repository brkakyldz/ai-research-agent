"""`ainews resummarize`: giving an article its body back, and rewriting from it.

Two things it answers. A reader marks a summary wrong on a fact, and the fix is
to hand the model the article again rather than to argue with the prompt. And a
row whose provenance is `unknown` - every article stored before the article and
the web context were separated (ADR 0029) - can be given one, because the URL is
still there.
"""

from __future__ import annotations

import itertools

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import publish

from ainews.config import Settings
from ainews.db import Article, BulletinItem, Source, Summary
from ainews.pipeline import repair as repair_module
from ainews.pipeline.nodes import summarize as summarize_module
from ainews.pipeline.repair import resummarize, unattributed_articles
from ainews.pipeline.state import ArticleSummary

REWRITTEN = ArticleSummary(
    title_local="Duzeltilmis baslik",
    summary="Makale yeniden okundu. Ozet degisti. Yeni olan govde.",
    why_it_matters="Artik makalenin kendisinden yazildi.",
    tags=["openai", "models"],
    importance=3,
)


class _Raw:
    """Just enough of a LangChain message for `usage_from_message` to cost it."""

    def __init__(self) -> None:
        self.usage_metadata = {"input_tokens": 100, "output_tokens": 40}
        self.response_metadata: dict[str, object] = {}


class _FakeStructured:
    async def ainvoke(self, _prompt: str, *_: object, **__: object) -> dict[str, object]:
        return {"parsed": REWRITTEN, "raw": _Raw(), "parsing_error": None}


class _FakeLLM:
    def with_structured_output(self, *_: object, **__: object) -> _FakeStructured:
        return _FakeStructured()


_seq = itertools.count(1)


async def _summarised(
    session: AsyncSession, *, body_source: str | None, languages: tuple[str, ...] = ("tr",)
) -> Article:
    n = next(_seq)
    source = Source(name=f"Source {n}", url=f"https://sw{n}.net/feed", weight=1.5)
    session.add(source)
    await session.flush()
    article = Article(
        source_id=source.id,
        title="A plugin release note",
        url=f"https://sw{n}.net/2026/plugin/",
        url_canonical=f"https://sw{n}.net/2026/plugin/",
        body_text="The stored body, whatever it is made of.",
        body_source=body_source,
    )
    session.add(article)
    await session.flush()
    ids: list[int] = []
    for language in languages:
        summary = Summary(
            article_id=article.id,
            language=language,
            title_local="Eski baslik",
            summary="Eski ozet.",
            why_it_matters="Eski gerekce.",
            importance=5,
            model="gpt-5.6-luna",
            tokens_in=1400,
            tokens_out=500,
            est_cost_usd=0.0008,
        )
        session.add(summary)
        await session.flush()
        if language == "tr":
            ids.append(summary.id)
    await session.commit()
    await publish(session, ids, tiers=["major"])
    return article


# -- what needs repairing -----------------------------------------------------


async def test_unattributed_lists_summarised_rows_with_unknown_provenance(
    session: AsyncSession,
) -> None:
    old = await _summarised(session, body_source="unknown")
    await _summarised(session, body_source="feed")

    assert await unattributed_articles(session) == [old.id]


async def test_an_unsummarised_article_is_not_a_repair_target(session: AsyncSession) -> None:
    """Nothing was written from it, so there is nothing to rewrite. `ainews
    prune` is what removes those."""
    source = Source(name="X", url="https://x.dev/feed")
    session.add(source)
    await session.flush()
    session.add(
        Article(
            source_id=source.id,
            title="Never summarised",
            url="https://x.dev/1",
            url_canonical="https://x.dev/1",
            body_text="Some text.",
            body_source="unknown",
        )
    )
    await session.commit()

    assert await unattributed_articles(session) == []


# -- the repair ---------------------------------------------------------------


async def test_a_repair_refetches_the_body_and_rewrites_the_summary(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    article = await _summarised(session, body_source="unknown")

    async def _fetch(*_: object, **__: object) -> str:
        return "The article as its own page serves it. " * 30

    monkeypatch.setattr(repair_module, "fetch_article", _fetch)
    monkeypatch.setattr(summarize_module, "summarizer", lambda *_, **__: _FakeLLM())

    result = await resummarize(session, article.id, settings)

    assert result.ok
    assert result.refetched
    assert result.body_source == "fetch"
    assert result.summaries_rewritten == 1

    await session.refresh(article)
    assert article.body_source == "fetch"
    # Whatever web context the old row carried is gone: the point of the repair
    # is a summary written from the article alone.
    assert article.extra_text is None

    summary = (await session.execute(Summary.__table__.select())).first()
    assert summary.title_local == "Duzeltilmis baslik"


async def test_the_repair_keeps_the_story_where_the_editor_put_it(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where a story sits is a fact about a bulletin (ADR 0030), and this
    rewrites a summary - so a repair has no way to re-order the day around one
    article even if it wanted to."""
    article = await _summarised(session, body_source="unknown")

    async def _fetch(*_: object, **__: object) -> str:
        return "A proper body. " * 40

    monkeypatch.setattr(repair_module, "fetch_article", _fetch)
    monkeypatch.setattr(summarize_module, "summarizer", lambda *_, **__: _FakeLLM())

    await resummarize(session, article.id, settings)

    item = (await session.execute(BulletinItem.__table__.select())).first()
    assert (item.position, item.tier) == (1, "major")
    # The summariser's own score is this call's to change.
    row = (await session.execute(Summary.__table__.select())).first()
    assert row.importance == 3


async def test_every_language_the_article_has_is_rewritten(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repair that fixed the Turkish bulletin and left the English one
    carrying the same wrong fact would be half a repair; both were written from
    the same body."""
    article = await _summarised(session, body_source="unknown", languages=("tr", "en"))

    async def _fetch(*_: object, **__: object) -> str:
        return "A proper body. " * 40

    monkeypatch.setattr(repair_module, "fetch_article", _fetch)
    monkeypatch.setattr(summarize_module, "summarizer", lambda *_, **__: _FakeLLM())

    result = await resummarize(session, article.id, settings)

    assert result.summaries_rewritten == 2


async def test_a_page_that_cannot_be_fetched_falls_back_to_the_stored_body(
    session: AsyncSession, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead link should not block the rewrite. The body keeps the provenance
    it had, which is the honest outcome: the text is the text it was."""
    article = await _summarised(session, body_source="unknown")

    async def _nothing(*_: object, **__: object) -> str:
        return ""

    monkeypatch.setattr(repair_module, "fetch_article", _nothing)
    monkeypatch.setattr(summarize_module, "summarizer", lambda *_, **__: _FakeLLM())

    result = await resummarize(session, article.id, settings)

    assert result.ok
    assert not result.refetched
    assert result.body_source == "unknown"
    assert result.summaries_rewritten == 1


async def test_a_missing_article_is_an_error_not_a_crash(
    session: AsyncSession, settings: Settings
) -> None:
    result = await resummarize(session, 9999, settings)
    assert not result.ok
    assert result.error == "no such article"


# -- the command --------------------------------------------------------------


def test_the_command_refuses_with_no_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    from ainews.cli import main

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from ainews.config import get_settings

    get_settings.cache_clear()
    try:
        assert main(["resummarize"]) == 2
    finally:
        get_settings.cache_clear()


def test_the_command_needs_a_key(monkeypatch: pytest.MonkeyPatch, env_free_of_keys: None) -> None:
    """It spends money per article, so it fails the way `digest` does."""
    from ainews.cli import main

    assert main(["resummarize", "1"]) == 2
