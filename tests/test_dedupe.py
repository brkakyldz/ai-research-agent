from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings
from ainews.db import Article, Source, Summary
from ainews.db.models import Run
from ainews.pipeline.nodes.dedupe import (
    dedupe_candidates,
    normalize_title,
    select_candidates,
    titles_match,
)


async def _source(session: AsyncSession, name: str = "S", weight: float = 1.0) -> Source:
    src = Source(name=name, url=f"https://{name.lower()}.dev/feed", weight=weight)
    session.add(src)
    await session.flush()
    return src


async def _article(
    session: AsyncSession, src: Source, title: str, slug: str, *, age_hours: float = 1.0
) -> Article:
    art = Article(
        source_id=src.id,
        title=title,
        url=f"https://{src.name.lower()}.dev/{slug}",
        url_canonical=f"https://{src.name.lower()}.dev/{slug}",
        fetched_at=datetime.now(UTC) - timedelta(hours=age_hours),
        published_at=datetime.now(UTC) - timedelta(hours=age_hours),
    )
    session.add(art)
    await session.flush()
    return art


# -- title normalisation ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("OpenAI ships GPT-5.6 Luna | TechCrunch", "openai ships gpt 5 6 luna"),
        ("OpenAI ships GPT-5.6 Luna - The Verge", "openai ships gpt 5 6 luna"),
        ("Show HN: A tiny RAG library", "a tiny rag library"),
        ("  Spaced   out   title  ", "spaced out title"),
    ],
)
def test_normalisation_strips_outlet_and_section_noise(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


# -- fuzzy matching -----------------------------------------------------------


async def test_the_same_story_from_three_outlets_survives_once(
    session: AsyncSession, settings: Settings
) -> None:
    a = await _source(session, "A")
    b = await _source(session, "B")
    c = await _source(session, "C")
    await _article(session, a, "OpenAI releases GPT-5.6 Luna, its cheapest model yet", "1")
    await _article(session, b, "OpenAI ships GPT-5.6 Luna, cheapest model yet", "2")
    await _article(session, c, "GPT-5.6 Luna released by OpenAI as its cheapest model", "3")
    await session.commit()

    survivors, stats = await dedupe_candidates(session, settings)

    assert stats.n_candidates == 3
    assert len(survivors) == 1, "three write-ups of one story should cost one summary"
    assert stats.n_duplicates == 2

    marked = (await session.execute(select(Article).where(Article.dup_of.isnot(None)))).scalars()
    assert all(m.dup_of == survivors[0] for m in marked)


async def test_different_stories_are_not_merged(session: AsyncSession, settings: Settings) -> None:
    src = await _source(session, "A")
    await _article(session, src, "OpenAI releases GPT-5.6 Luna", "1")
    await _article(session, src, "Anthropic publishes interpretability research", "2")
    await _article(session, src, "Hugging Face raises a Series E", "3")
    await session.commit()

    survivors, stats = await dedupe_candidates(session, settings)
    assert len(survivors) == 3
    assert stats.n_duplicates == 0


async def test_duplicates_are_marked_not_deleted(session: AsyncSession, settings: Settings) -> None:
    """A wrong merge has to stay inspectable instead of becoming a missing row."""
    a = await _source(session, "A")
    b = await _source(session, "B")
    await _article(session, a, "Meta open-sources a new speech model", "1")
    await _article(session, b, "Meta open sources new speech model", "2")
    await session.commit()

    await dedupe_candidates(session, settings)
    assert len((await session.execute(select(Article))).scalars().all()) == 2


async def test_a_candidate_matching_an_already_summarised_article_is_dropped(
    session: AsyncSession, settings: Settings
) -> None:
    """Yesterday's story resurfacing in a slow feed must not be summarised twice."""
    a = await _source(session, "A")
    b = await _source(session, "B")
    old = await _article(session, a, "Google DeepMind announces Gemini 4", "1", age_hours=20)
    run = Run(kind="digest", language="tr")
    session.add(run)
    await session.flush()
    session.add(
        Summary(
            article_id=old.id,
            run_id=run.id,
            language="tr",
            title_local="x",
            summary="y",
            why_it_matters="z",
        )
    )
    await _article(session, b, "Gemini 4 announced by Google DeepMind", "2")
    await session.commit()

    survivors, stats = await dedupe_candidates(session, settings)
    assert survivors == []
    assert stats.n_duplicates == 1


async def test_articles_outside_the_lookback_window_do_not_suppress_new_ones(
    session: AsyncSession, settings: Settings
) -> None:
    """A story genuinely re-reported a week later is news again, not a duplicate."""
    a = await _source(session, "A")
    b = await _source(session, "B")
    stale = await _article(
        session,
        a,
        "EU AI Act enforcement begins",
        "1",
        age_hours=settings.dedupe_lookback_hours + 24,
    )
    run = Run(kind="digest", language="tr")
    session.add(run)
    await session.flush()
    session.add(
        Summary(
            article_id=stale.id,
            run_id=run.id,
            language="tr",
            title_local="x",
            summary="y",
            why_it_matters="z",
        )
    )
    await _article(session, b, "EU AI Act enforcement begins", "2")
    await session.commit()

    survivors, _ = await dedupe_candidates(session, settings)
    assert len(survivors) == 1


# -- candidate selection ------------------------------------------------------


async def test_already_summarised_articles_are_never_candidates_again(
    session: AsyncSession, settings: Settings
) -> None:
    """This is what makes "Run now" a delta: pressing it twice costs nothing twice."""
    src = await _source(session, "A")
    art = await _article(session, src, "A one-off story", "1")
    await session.commit()

    assert [a.id for a in await select_candidates(session, settings)] == [art.id]

    run = Run(kind="digest", language="tr")
    session.add(run)
    await session.flush()
    session.add(
        Summary(
            article_id=art.id,
            run_id=run.id,
            language="tr",
            title_local="x",
            summary="y",
            why_it_matters="z",
        )
    )
    await session.commit()

    assert await select_candidates(session, settings) == []


async def test_empty_input_is_not_an_error(session: AsyncSession, settings: Settings) -> None:
    survivors, stats = await dedupe_candidates(session, settings)
    assert survivors == []
    assert stats.n_candidates == 0


# -- the two bugs the 2026-09-04 run exposed (PLAN-EVALS E0.1, E0.2) -----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # A hyphen inside a product name is not an outlet separator.
        ("OpenAI begins rolling out GPT-6 Astra", "openai begins rolling out gpt 6 astra"),
        ("llm-gemini 0.34", "llm gemini 0 34"),
        # A spaced dash still is, in all three widths.
        ("Nvidia is buying Hugging Face - The Verge", "nvidia is buying hugging face"),
        ("Nvidia is buying Hugging Face – The Verge", "nvidia is buying hugging face"),
        ("Nvidia is buying Hugging Face — The Verge", "nvidia is buying hugging face"),
        # A feed's HTML entities are punctuation, not tokens.
        (
            "Sam Altman apologizes for &#8216;messy&#8217; GPT-6 Astra rollout",
            "sam altman apologizes for messy gpt 6 astra rollout",
        ),
    ],
)
def test_a_hyphenated_product_name_is_not_an_outlet_suffix(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


async def test_a_short_title_is_not_a_duplicate_of_a_long_one_containing_it(
    session: AsyncSession, settings: Settings
) -> None:
    """`token_set_ratio` scores a subset at 100, so "Fable 5.1" matched every
    long headline that mentioned it. Both directions have to survive: the short
    title arriving after the long one, and the long one arriving after it."""
    a = await _source(session, "A")
    b = await _source(session, "B")
    await _article(session, a, "Claude Fable 5.1 made me a really nice animated pelican", "1")
    await _article(session, b, "Fable 5.1", "2")
    await _article(session, a, "GPT-6 Astra", "3")
    await _article(session, b, "Sam Altman apologizes for messy GPT-6 Astra rollout", "4")
    await session.commit()

    survivors, stats = await dedupe_candidates(session, settings)
    assert len(survivors) == 4
    assert stats.n_duplicates == 0


async def test_a_short_title_still_matches_its_exact_twin(
    session: AsyncSession, settings: Settings
) -> None:
    a = await _source(session, "A")
    b = await _source(session, "B")
    await _article(session, a, "Fable 5.1", "1")
    await _article(session, b, "Fable 5.1", "2")
    await session.commit()

    survivors, stats = await dedupe_candidates(session, settings)
    assert len(survivors) == 1
    assert stats.n_duplicates == 1


# -- golden pairs (PLAN-EVALS E0.3) --------------------------------------------
#
# Real headlines from the 2026-09-04 run, with the verdict a reader gave them.
# The rows the fuzzy matcher cannot reach are marked `xfail(strict=True)`: they
# are the same event written up by different outlets with different words, and
# they are the measurement behind the E5 trigger ("golden pairs keep failing on
# new outlets -> embedding dedupe"). When one of them starts passing, the mark
# comes off and the trigger is re-read.

_BEYOND_FUZZY = pytest.mark.xfail(
    strict=True, reason="same event, different words: beyond token_set_ratio (E5 trigger)"
)

GOLDEN_PAIRS = [
    # -- should merge, and does
    (
        "Proactive cyber defense for governments and enterprises",
        "Proactive cyber defense for governments and enterprises",
        True,
    ),
    (
        "Nvidia is buying Hugging Face for almost $13 billion - The Verge",
        "Nvidia is buying Hugging Face for almost $13 billion",
        True,
    ),
    (
        "OpenAI launches Astra, its powerful (and controversial) new model | TechCrunch",
        "OpenAI launches Astra, its powerful (and controversial) new model",
        True,
    ),
    (
        "Nvidia buys Hugging Face, the GitHub of AI, for $13 billion",
        "For $13 billion, Nvidia buys Hugging Face, the GitHub of AI",
        True,
    ),
    (
        "Google releases Gemini 3.8 Flash, its third Flash model in six weeks",
        "Google releases Gemini 3.8 Flash — its third Flash model in six weeks",
        True,
    ),
    (
        "Sam Altman apologizes for &#8216;messy&#8217; GPT-6 Astra rollout "
        "that’s locked out paying users",
        "Sam Altman apologizes for ‘messy’ GPT-6 Astra rollout that’s locked out paying users",
        True,
    ),
    (
        "Show HN: Give Your Coding Agents a Memory You Own",
        "Give Your Coding Agents a Memory You Own",
        True,
    ),
    (
        "OpenAI begins rolling out GPT-6 Astra",
        "OpenAI begins rolling out GPT-6 Astra — Hacker News",
        True,
    ),
    # -- the two wrong merges, now refused
    ("Claude Fable 5.1 made me a really nice animated pelican", "Fable 5.1", False),
    ("BenchMIRT: What are LLM benchmarks actually measuring?", "llm-gemini 0.34", False),
    # -- a product name shared by different stories is not one story
    (
        "GPT‑6 Astra",
        "Sam Altman apologizes for ‘messy’ GPT-6 Astra rollout that’s locked out paying users",
        False,
    ),
    ("GPT‑6 Astra", "OpenAI launches Astra, its powerful (and controversial) new model", False),
    (
        "Legora reviewed 41 documents in minutes with GPT-6 Astra",
        "Playco cut manual fixes 50% prototyping games with GPT-6 Astra",
        False,
    ),
    (
        "Safety overview: GPT-6 Astra",
        "Path to Astra: critical capabilities and frontier safeguards",
        False,
    ),
    (
        "OpenAI launches Astra, its powerful (and controversial) new model",
        "OpenAI’s next big AI model has ‘entered the AGI era’",
        False,
    ),
    (
        "OpenAI’s new reasoning technique alarms AI safety experts",
        "OpenAI’s next big AI model has ‘entered the AGI era’",
        False,
    ),
    # -- true negatives: one company, one topic, two stories
    (
        "Nvidia confirms it will buy Hugging Face for $12.9 billion",
        "The Hugging Face hack could indicate cultural issues at OpenAI",
        False,
    ),
    (
        "Nvidia confirms it will buy Hugging Face for $12.9 billion",
        "Nvidia launches free tool that links idle computers into a personal AI data center",
        False,
    ),
    (
        "Import AI 471: Why Hugging Face worries me; space mining; FIve Eyes on AI",
        "The Hugging Face hack could indicate cultural issues at OpenAI",
        False,
    ),
    ("Give Your Coding Agents a Memory You Own", "Codex bundles LibreOffice", False),
    (
        "Trump may be forced to reveal secret rules feds use for AI safety testing",
        'Trump blacklisting of "woke" Anthropic deemed illegal by federal judge',
        False,
    ),
    (
        "Meta is paying to peek at how you use their latest AI model",
        "Inside Meta’s push to put robots to work in data centers",
        False,
    ),
    (
        "ChatGPT and Reddit now face EU's toughest online safety rules",
        "US government sides with OpenAI on issue of training LLMs on copyrighted material",
        False,
    ),
    (
        "NeoMME: an efficient Multimodal-native and Multilingual Encoder",
        "Fine-tuning a 350M Model for Better Structured Outputs in 100 GRPO Steps",
        False,
    ),
    (
        "Daybreak for Frontline Defenders: $1B to protect essential services",
        "OpenAI supports California’s bill to advance youth AI safety",
        False,
    ),
    (
        "Accel reportedly in talks to lead $1B round for Thinking Machines at $40B valuation",
        "Wonderful more than doubles its valuation to $5B in under 6 months",
        False,
    ),
    (
        "Introducing WeatherNext 3, our most advanced and accurate global weather AI model",
        "Introducing agentic video understanding with Gemini",
        False,
    ),
    (
        "Crusoe reportedly raises $3B at a  $30B valuation",
        "HiddenLayer nabs $100M as enterprises rush to secure their AI deployments",
        False,
    ),
    ("Quoting Rick Brewster", "Quoting Tarn Adams", False),
    ("Introducing wrapture", "Introducing Hy4 Preview", False),
    # -- should merge, and cannot yet: the same event in different words
    pytest.param(
        "Nvidia confirms it will buy Hugging Face for $12.9 billion",
        "Nvidia is buying Hugging Face for almost $13 billion",
        True,
        marks=_BEYOND_FUZZY,
    ),
    pytest.param(
        "Nvidia confirms it will buy Hugging Face for $12.9 billion",
        "Nvidia buys Hugging Face, the GitHub of AI, for $13 billion",
        True,
        marks=_BEYOND_FUZZY,
    ),
    pytest.param(
        "Nvidia is buying Hugging Face for almost $13 billion",
        "Nvidia buys Hugging Face, the GitHub of AI, for $13 billion",
        True,
        marks=_BEYOND_FUZZY,
    ),
    pytest.param(
        "Introducing Gemini 3.8 Flash and 3.8 Flash Cyber",
        "Google releases Gemini 3.8 Flash, its third Flash model in six weeks",
        True,
        marks=_BEYOND_FUZZY,
    ),
    pytest.param(
        "Google says its new Gemini 3.8 Flash model ‘works harder’ but might cost more",
        "Google releases Gemini 3.8 Flash, its third Flash model in six weeks",
        True,
        marks=_BEYOND_FUZZY,
    ),
    pytest.param(
        "Google’s latest AI weather model gives you no excuse to forget your umbrella",
        "Google says its AI weather model is getting better",
        True,
        marks=_BEYOND_FUZZY,
    ),
    pytest.param(
        "Introducing WeatherNext 3, our most advanced and accurate global weather AI model",
        "Google says its AI weather model is getting better",
        True,
        marks=_BEYOND_FUZZY,
    ),
    pytest.param(
        "OpenAI begins rolling out GPT-6 Astra",
        "OpenAI launches Astra, its powerful (and controversial) new model",
        True,
        marks=_BEYOND_FUZZY,
    ),
    pytest.param(
        "ChatGPT, Grok, and Claude all went down at the same time",
        "Four major AI models suffer rare overlapping downtime",
        True,
        marks=_BEYOND_FUZZY,
    ),
]


@pytest.mark.parametrize(("title_a", "title_b", "should_merge"), GOLDEN_PAIRS)
def test_dedupe_golden_pairs(
    title_a: str, title_b: str, should_merge: bool, settings: Settings
) -> None:
    verdict = titles_match(
        normalize_title(title_a), normalize_title(title_b), settings.dedupe_score_threshold
    )
    assert verdict is should_merge


# -- the survivor rule (2026-09-08) --------------------------------------------


async def test_the_primary_source_survives_the_rewrite(
    session: AsyncSession, settings: Settings
) -> None:
    """OpenAI posts at 09:00 at weight 2.0; TechCrunch rewrites it at 11:00 at 1.0.

    The candidate list used to be newest-first, so the rewrite was the first of
    the pair the loop saw, survived, and the primary source was marked as *its*
    duplicate. The rank prompt's rule - the representative of an event is the
    primary source's item - then had nothing to apply to, because the primary
    source never reached the ranker.
    """
    lab = await _source(session, "OpenAI", weight=2.0)
    press = await _source(session, "TechCrunch", weight=1.0)
    original = await _article(session, lab, "OpenAI ships GPT-5.6 Luna, its cheapest model", "1")
    rewrite = await _article(
        session, press, "OpenAI ships GPT-5.6 Luna, its cheapest model yet", "2", age_hours=0.5
    )
    await session.commit()

    survivors, stats = await dedupe_candidates(session, settings)

    assert survivors == [original.id]
    assert stats.n_duplicates == 1
    await session.refresh(rewrite)
    assert rewrite.dup_of == original.id


async def test_among_equal_sources_the_earliest_write_up_survives(
    session: AsyncSession, settings: Settings
) -> None:
    """Same weight: the original is the one that was published first."""
    a = await _source(session, "A")
    b = await _source(session, "B")
    first = await _article(session, a, "Meta open-sources a new speech model", "1", age_hours=3)
    later = await _article(session, b, "Meta open sources new speech model", "2", age_hours=1)
    await session.commit()

    survivors, _ = await dedupe_candidates(session, settings)
    assert survivors == [first.id]
    await session.refresh(later)
    assert later.dup_of == first.id
