"""Two prompts over one frozen corpus (PLAN-V2 5.7), offline.

The command a person who did not write this application needs before they can
change a prompt: without it, "is this wording better" is answered by pressing
the button, paying for a run and reading fifteen summaries about a different
day's news.

Every test here fakes the model. What is being tested is that the two sides see
the same bodies, that the checks are run over both answers, and that the cost
guard refuses before the first call - none of which needs a real one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings
from ainews.db import Article, Source
from ainews.evals import compare as compare_module
from ainews.evals.compare import (
    CorpusItem,
    CostGuard,
    compare,
    format_comparison,
    freeze_corpus,
    read_corpus,
    write_corpus,
)
from ainews.pipeline.state import ArticleSummary

BODY = "Nvidia will pay $12.9 billion for Hugging Face, which hosts 3 million models. " * 10


class FakeMessage:
    def __init__(self) -> None:
        self.usage_metadata = {"input_tokens": 1200, "output_tokens": 200}
        self.response_metadata: dict[str, Any] = {}


class FakeStructured:
    def __init__(self, answer: Any, calls: list[str]) -> None:
        self._answer = answer
        self._calls = calls

    async def ainvoke(self, prompt: str, *_: object, **__: object) -> dict[str, Any]:
        self._calls.append(prompt)
        return {"parsed": self._answer(prompt), "raw": FakeMessage()}


class FakeLLM:
    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls: list[str] = []

    def with_structured_output(self, _schema: Any, **__: object) -> FakeStructured:
        return FakeStructured(self.answer, self.calls)


def _items(n: int = 3) -> list[CorpusItem]:
    return [
        CorpusItem(
            article_id=i,
            source="Ars",
            title=f"Nvidia buys Hugging Face {i}",
            url=f"https://ars.dev/{i}",
            published="2026-09-08T09:00:00+00:00",
            body=BODY,
        )
        for i in range(n)
    ]


def _answer(strong: bool) -> Any:
    """Two summarisers: one that names the figure, one that writes around it."""

    def answer(prompt: str) -> ArticleSummary:
        if strong:
            return ArticleSummary(
                title_local="Nvidia 12,9 milyar dolara Hugging Face'i aldi",
                summary=(
                    "Nvidia 12,9 milyar dolar odedi. Hugging Face 3 milyon model barindiriyor. Uc."
                ),
                why_it_matters="Model dagitimi tek elde toplaniyor.",
                tags=["nvidia", "hugging-face"],
                importance=5,
                key_fact="12,9 milyar dolar",
            )
        return ArticleSummary(
            title_local="Sektorde onemli bir gelisme",
            summary="Bir sirket bir baskasini satin aldi. Sektor icin onemli. Takip edilmeli.",
            why_it_matters="Onemli olabilir.",
            tags=["yapay-zeka-haberleri"],
            importance=3,
            key_fact="12,9 milyar dolar",
        )

    return answer


# -- the corpus ---------------------------------------------------------------


async def test_the_corpus_takes_only_bodies_that_can_be_attributed(
    session: AsyncSession,
) -> None:
    """An `unknown` body may be the article with a week's search results glued
    to it (ADR 0029). Comparing two prompts over one of those measures two
    readings of a text neither prompt was written for."""
    src = Source(name="Ars", url="https://ars.dev/feed")
    session.add(src)
    await session.flush()
    for index, provenance in enumerate(("feed", "fetch", "unknown")):
        session.add(
            Article(
                source_id=src.id,
                title=f"Story {index}",
                url=f"https://ars.dev/{index}",
                url_canonical=f"https://ars.dev/{index}",
                body_text=BODY,
                body_source=provenance,
            )
        )
    # And one too short to be worth a comparison.
    session.add(
        Article(
            source_id=src.id,
            title="Stub",
            url="https://ars.dev/stub",
            url_canonical="https://ars.dev/stub",
            body_text="Two words.",
            body_source="feed",
        )
    )
    await session.commit()

    items = await freeze_corpus(session, size=40)
    assert {item.title for item in items} == {"Story 0", "Story 1"}


async def test_the_same_seed_cuts_the_same_corpus(session: AsyncSession) -> None:
    src = Source(name="Ars", url="https://ars.dev/feed")
    session.add(src)
    await session.flush()
    for index in range(12):
        session.add(
            Article(
                source_id=src.id,
                title=f"Story {index}",
                url=f"https://ars.dev/{index}",
                url_canonical=f"https://ars.dev/{index}",
                body_text=BODY,
                body_source="feed",
            )
        )
    await session.commit()

    first = await freeze_corpus(session, size=5, seed=1)
    again = await freeze_corpus(session, size=5, seed=1)
    other = await freeze_corpus(session, size=5, seed=99)
    assert [i.article_id for i in first] == [i.article_id for i in again]
    assert [i.article_id for i in first] != [i.article_id for i in other]


def test_a_corpus_round_trips_through_the_file(tmp_path: Path) -> None:
    path = write_corpus(_items(2), tmp_path / "c.jsonl")
    assert read_corpus(path) == _items(2)


def test_a_missing_corpus_says_how_to_make_one(tmp_path: Path) -> None:
    with pytest.raises(LookupError, match="ainews eval corpus"):
        read_corpus(tmp_path / "nothing.jsonl")


# -- the comparison -----------------------------------------------------------


async def test_both_prompts_see_the_same_bodies(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the frozen corpus. Two runs a day apart are two days of
    news; two prompts over one corpus differ only in the prompt."""
    llm = FakeLLM(_answer(strong=True))
    monkeypatch.setattr(compare_module, "summarizer", lambda *_: llm)
    candidate = tmp_path / "candidate.md"
    candidate.write_text(
        "CANDIDATE {source} {title} {url} {published} {body}{extra} {tags}", "utf-8"
    )

    result = await compare(
        _items(3), prompt_a=None, prompt_b=candidate, language="tr", settings=settings
    )

    assert len(llm.calls) == 6, "three bodies, twice"
    shipped = [c for c in llm.calls if not c.startswith("CANDIDATE")]
    candidate_calls = [c for c in llm.calls if c.startswith("CANDIDATE")]
    assert len(shipped) == len(candidate_calls) == 3
    for call in candidate_calls:
        assert "Nvidia will pay $12.9 billion" in call, "the candidate got the body too"
    assert result.a.prompt_version != result.b.prompt_version


async def test_the_comparison_measures_the_writing_and_names_no_winner(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prompt that drops the figure scores worse on the content floor and no
    worse on anything that measures shape - which is the whole argument for
    `content_floor` existing (PLAN-V2 5.6)."""
    strong, weak = FakeLLM(_answer(strong=True)), FakeLLM(_answer(strong=False))
    sides = iter((strong, weak))
    monkeypatch.setattr(compare_module, "summarizer", lambda *_: next(sides))
    candidate = tmp_path / "candidate.md"
    candidate.write_text("B {source}{title}{url}{published}{body}{extra}{tags}", "utf-8")

    result = await compare(
        _items(3), prompt_a=None, prompt_b=candidate, language="tr", settings=settings
    )
    text = format_comparison(result)

    assert "a body figure reached the summary" in text
    assert "100%" in text and "0%" in text, "one side kept the figure and the other did not"
    # No verdict. The measures trade against each other - a prompt that names
    # more figures also breaks the word budget more - and picking for the reader
    # would hide the trade the command exists to show.
    assert "winner" not in text.lower() and "better" not in text.lower()


async def test_the_estimate_refuses_before_the_first_call(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard the judge has, for the same reason: this makes two calls per
    body and the corpus is forty of them."""
    llm = FakeLLM(_answer(strong=True))
    monkeypatch.setattr(compare_module, "summarizer", lambda *_: llm)
    candidate = tmp_path / "candidate.md"
    candidate.write_text("B {source}{title}{url}{published}{body}{extra}{tags}", "utf-8")

    with pytest.raises(CostGuard, match="above --max-cost"):
        await compare(
            _items(40),
            prompt_a=None,
            prompt_b=candidate,
            language="tr",
            max_cost=0.0001,
            settings=settings,
        )
    assert llm.calls == [], "nothing was spent"
