"""The dedupe step: fuzzy title matching, after the URL index has done its half.

Collection already stores a syndicated article once, because the canonical URL is
a unique key. What it cannot catch is the same story written up separately by
five outlets - "OpenAI releases GPT-5.6 Luna", "OpenAI ships new cheap model",
"GPT-5.6 Luna is here" - which are three URLs, three rows, and one story. Without
this step the digest spends three summaries and three slots on it.

The comparison is `token_set_ratio`, not plain ratio, because the failure mode
here is a headline that adds or drops words rather than misspelling them: token
set ignores order and duplication and scores on the shared vocabulary, which is
exactly the shape of "OpenAI ships X" versus "X shipped by OpenAI today".

Duplicates are marked, never deleted. `dup_of` points at the survivor, so a wrong
merge stays visible in the database instead of becoming a missing article nobody
can explain.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

from rapidfuzz import fuzz, process
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings, get_settings
from ainews.db import Article, Source, Summary
from ainews.db.models import utcnow

log = logging.getLogger(__name__)

# Outlets append their own name to the headline; two feeds carrying one story
# would otherwise differ by exactly that suffix.
# The en and em dashes are deliberate: real headlines separate with them.
# Whitespace is required on *both* sides of the separator. With `\s*` the
# hyphen inside a product name counted as one: "OpenAI begins rolling out
# GPT-6 Astra" normalised to `openai begins rolling out gpt` and "llm-gemini
# 0.34" to `llm`, which then merged with an unrelated benchmark post
# (2026-09-04 run; reports/research/2026-09-05_quality-evaluation.md).
_SOURCE_SUFFIX = re.compile(r"\s+[|–—-]\s+[\w .'&]{2,30}$")
# Section prefixes from aggregators, which say nothing about the story.
_PREFIX = re.compile(r"^\s*(show hn|ask hn|tell hn|launch hn|video|watch|opinion)\s*:\s*", re.I)
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


# Below this many tokens a title is a name, not a sentence - "Fable 5.1",
# "GPT-6 Astra" - and `token_set_ratio` scores any long headline containing
# those words at 100. Such a title matches only its exact twin.
MIN_TITLE_TOKENS = 4

# How many matches above the threshold to consider before giving up on a
# candidate. Five, because the veto below can refuse the best one and the story
# a headline really duplicates is not usually its sixth-closest neighbour.
MATCH_CANDIDATES = 5

# Words that reverse a headline. Normalisation strips punctuation and lowercases,
# so `won't` arrives as `won t` and the bare `t` is not worth matching on; the
# `not` in "will not" carries it instead.
_NEGATIONS = frozenset(
    {
        "no",
        "not",
        "never",
        "without",
        "deny",
        "denies",
        "denied",
        "reject",
        "rejects",
        "rejected",
        "pull",
        "pulls",
        "pulled",
        "halt",
        "halts",
        "halted",
        "cancel",
        "cancels",
        "cancelled",
        "canceled",
        "abandon",
        "abandons",
        "abandoned",
        "delay",
        "delays",
        "delayed",
    }
)


def normalize_title(title: str) -> str:
    """Reduce a headline to the words that carry the story."""
    # Feeds ship entities verbatim ("&#8216;messy&#8217;"); unescaped first so
    # the quote is punctuation to strip rather than the tokens "8216" and "8217".
    text = _PREFIX.sub("", html.unescape(title or "")).strip()
    text = _SOURCE_SUFFIX.sub("", text)
    text = _NON_WORD.sub(" ", text.lower())
    return _SPACE.sub(" ", text).strip()


def is_short_title(normalized: str) -> bool:
    return len(normalized.split()) < MIN_TITLE_TOKENS


def _numeric_tokens(normalized: str) -> set[str]:
    return {token for token in normalized.split() if any(c.isdigit() for c in token)}


def contradicts(a: str, b: str) -> bool:
    """Whether two *normalised* titles disagree about a fact, however alike.

    `token_set_ratio` measures shared vocabulary, and two headlines about
    opposite events share almost all of it. Measured against this repository's
    own matcher at the configured threshold of 85: a version bump scores 98
    ("GPT-5.6 Luna" against "GPT-5.7 Luna"), a funding round 97 ("$5B at $60B"
    against "$15B at $160B"), a denial 93 ("confirms it will buy" against
    "denies it will buy"), a withdrawal 87 ("launches to Plus users" against
    "pulls from Plus users") and consecutive issues of a newsletter 87. In every
    one the later article was marked a duplicate and never summarised, and the
    survivor is the heaviest source - so a lab's announcement of the first model
    silenced the press's report of the second, and a claim silenced its denial.

    Two vetoes, both cheap:

    **Numbers that conflict.** Not "the numbers differ": one headline naming a
    figure the other omits is the ordinary case of two outlets covering one
    event, so a subset is allowed. A veto needs each side to carry a number the
    other does not - `{5, 6}` against `{5, 7}`, `{343}` against `{342}`.

    **A negation on one side only.** "denies", "not", "pulls" and their kin
    appearing in one title and not the other.

    What it does not catch is worth naming, because a guard read as complete is
    worse than one read as partial: "Gemini 3.8 Flash" against "Gemini 3.8 Pro"
    has identical numbers and no negation, and "EU fines OpenAI 100 million"
    against "EU fines Meta 100 million" differs only by subject. Both still
    merge. Those are recall problems for an embedding, not precision problems
    for a string comparison.
    """
    numbers_a, numbers_b = _numeric_tokens(a), _numeric_tokens(b)
    if not (numbers_a <= numbers_b or numbers_b <= numbers_a):
        return True
    words_a, words_b = set(a.split()), set(b.split())
    return (_NEGATIONS & words_a) != (_NEGATIONS & words_b)


def titles_match(a: str, b: str, threshold: int) -> bool:
    """The pairwise decision, on two *normalised* titles.

    The loop below asks the same question through `process.extract` for speed;
    this function is the reference the golden-pair test holds it to. A short
    title on either side is only ever its own duplicate: the fuzzy score is a
    subset test in disguise there.
    """
    if not a or not b:
        return False
    if is_short_title(a) or is_short_title(b):
        return a == b
    if contradicts(a, b):
        return False
    return fuzz.token_set_ratio(a, b) >= threshold


@dataclass(slots=True)
class DedupeStats:
    n_candidates: int = 0
    n_duplicates: int = 0
    pairs: list[tuple[int, int, float]] = field(default_factory=list)


def _unsummarized(settings: Settings) -> Select[tuple[Article]]:
    """Articles that have never been summarised, in any language, in any run.

    This is what makes "Run now" a delta rather than a repeat: an article that
    already carries a summary is simply not a candidate again, so the button can
    be pressed twice in a row without paying for the same stories twice.

    The order is the survivor rule. `dedupe_candidates` walks this list once
    and the first article of a cluster is the one that stays; the rest are
    marked `dup_of` it and never reach the ranker. Newest-first would choose the
    *last* outlet to write a story up: OpenAI posts at 09:00 at weight 2.0,
    TechCrunch rewrites it at 11:00 at 1.0, and the rewrite survives while the
    primary source is marked as its duplicate. The rank prompt's rule - the
    representative of an event is the primary source's item - then never fires,
    because the primary source has been
    dropped one node earlier. Heaviest source first, and among equals the
    earliest published, which is the original by definition.
    """
    horizon = utcnow() - timedelta(days=settings.collect_max_age_days)
    return (
        select(Article)
        .join(Source, Source.id == Article.source_id)
        .where(Article.dup_of.is_(None))
        .where(Article.fetched_at >= horizon)
        .where(~select(Summary.id).where(Summary.article_id == Article.id).exists())
        .order_by(
            Source.weight.desc(),
            Article.published_at.asc().nullslast(),
            Article.id.asc(),
        )
    )


async def select_candidates(
    session: AsyncSession, settings: Settings | None = None
) -> list[Article]:
    settings = settings or get_settings()
    return list((await session.execute(_unsummarized(settings))).scalars())


async def count_candidates(session: AsyncSession, settings: Settings | None = None) -> int:
    """How many articles the next press would summarise - `select_candidates`
    without the rows.

    The confirmation fragment on `/runs` asks this on every render, and every
    model-slot click re-renders it. It used to be `len(await
    select_candidates(...))`, which loaded a hundred and thirty `Article`
    objects, mapped every column of each, and took the length. The order the
    query carries is the survivor rule and a count does not need it either.
    """
    settings = settings or get_settings()
    return (
        await session.execute(
            select(func.count()).select_from(_unsummarized(settings).order_by(None).subquery())
        )
    ).scalar_one()


async def _reference_titles(
    session: AsyncSession, settings: Settings, exclude: set[int]
) -> list[tuple[int, str]]:
    """Recently seen articles a candidate might be restating.

    Only the ones we have already summarised or already kept: matching against
    another candidate happens inside the loop below, in arrival order.
    """
    horizon = utcnow() - timedelta(hours=settings.dedupe_lookback_hours)
    rows = await session.execute(
        select(Article.id, Article.title)
        .where(Article.fetched_at >= horizon)
        .where(Article.dup_of.is_(None))
        .where(Article.id.notin_(exclude) if exclude else True)
    )
    return [(rid, normalize_title(title)) for rid, title in rows if title]


async def dedupe_candidates(
    session: AsyncSession, settings: Settings | None = None
) -> tuple[list[int], DedupeStats]:
    """Mark restatements and return the ids worth summarising."""
    settings = settings or get_settings()
    candidates = await select_candidates(session, settings)
    stats = DedupeStats(n_candidates=len(candidates))
    if not candidates:
        return [], stats

    candidate_ids = {a.id for a in candidates}
    reference = await _reference_titles(session, settings, exclude=candidate_ids)
    # Two pools, because the two kinds of title are matched differently: a
    # sentence-length headline goes through the fuzzy scorer, a name-length one
    # ("Fable 5.1") is looked up exactly. Keeping the short ones out of the fuzzy
    # pool is what stops a long candidate matching a short reference at 100.
    ref_ids: list[int] = []
    ref_titles: list[str] = []
    short_refs: dict[str, int] = {}
    for rid, title in reference:
        if is_short_title(title):
            short_refs.setdefault(title, rid)
        else:
            ref_ids.append(rid)
            ref_titles.append(title)

    survivors: list[int] = []
    for article in candidates:
        title = normalize_title(article.title)
        if not title:
            survivors.append(article.id)
            continue

        if is_short_title(title):
            twin = short_refs.get(title)
            if twin is not None:
                article.dup_of = twin
                stats.n_duplicates += 1
                stats.pairs.append((article.id, twin, 100.0))
                continue
            survivors.append(article.id)
            short_refs[title] = article.id
            continue

        # `extract` rather than `extractOne`, so a vetoed best match does not
        # hide a valid weaker one: "GPT-5.7 Luna released" scores highest
        # against last week's "GPT-5.6 Luna released", which `contradicts`
        # refuses, and second against this morning's "OpenAI ships GPT-5.7
        # Luna", which is the merge that should happen.
        matches = process.extract(
            title,
            ref_titles,
            scorer=fuzz.token_set_ratio,
            score_cutoff=settings.dedupe_score_threshold,
            limit=MATCH_CANDIDATES,
        )
        merged = False
        for candidate_title, score, index in matches:
            if contradicts(title, candidate_title):
                continue
            article.dup_of = ref_ids[index]
            stats.n_duplicates += 1
            stats.pairs.append((article.id, ref_ids[index], score))
            merged = True
            break
        if merged:
            continue

        # The survivor joins the reference set, so the third outlet covering the
        # same story matches the first one rather than sliding through.
        survivors.append(article.id)
        ref_ids.append(article.id)
        ref_titles.append(title)

    await session.commit()
    log.info(
        "dedupe: %d candidates, %d duplicates marked, %d to summarise",
        stats.n_candidates,
        stats.n_duplicates,
        len(survivors),
    )
    return survivors, stats
