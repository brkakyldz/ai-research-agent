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

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

from rapidfuzz import fuzz, process
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings, get_settings
from ainews.db import Article, Summary
from ainews.db.models import utcnow

log = logging.getLogger(__name__)

# Outlets append their own name to the headline; two feeds carrying one story
# would otherwise differ by exactly that suffix.
# The en and em dashes are deliberate: real headlines separate with them.
_SOURCE_SUFFIX = re.compile(r"\s*[|–—-]\s*[\w .'&]{2,30}$")  # noqa: RUF001
# Section prefixes from aggregators, which say nothing about the story.
_PREFIX = re.compile(r"^\s*(show hn|ask hn|tell hn|launch hn|video|watch|opinion)\s*:\s*", re.I)
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Reduce a headline to the words that carry the story."""
    text = _PREFIX.sub("", title or "").strip()
    text = _SOURCE_SUFFIX.sub("", text)
    text = _NON_WORD.sub(" ", text.lower())
    return _SPACE.sub(" ", text).strip()


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
    """
    horizon = utcnow() - timedelta(days=settings.collect_max_age_days)
    return (
        select(Article)
        .where(Article.dup_of.is_(None))
        .where(Article.fetched_at >= horizon)
        .where(~select(Summary.id).where(Summary.article_id == Article.id).exists())
        .order_by(Article.published_at.desc().nullslast(), Article.id.desc())
    )


async def select_candidates(
    session: AsyncSession, settings: Settings | None = None
) -> list[Article]:
    settings = settings or get_settings()
    return list((await session.execute(_unsummarized(settings))).scalars())


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
    ref_ids = [rid for rid, _ in reference]
    ref_titles = [title for _, title in reference]

    survivors: list[int] = []
    for article in candidates:
        title = normalize_title(article.title)
        if not title:
            survivors.append(article.id)
            continue

        match = process.extractOne(
            title,
            ref_titles,
            scorer=fuzz.token_set_ratio,
            score_cutoff=settings.dedupe_score_threshold,
        )
        if match is not None:
            _, score, index = match
            article.dup_of = ref_ids[index]
            stats.n_duplicates += 1
            stats.pairs.append((article.id, ref_ids[index], score))
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
