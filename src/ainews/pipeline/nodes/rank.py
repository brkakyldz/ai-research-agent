"""The rank step: three readings of a whole day, aggregated into one bulletin.

Two things separate this from the node it replaces.

**It selects rather than fills.** `top_n` is a ceiling, not a target: a thin day
returns four stories and says so, instead of the model being asked for fifteen
and finding fifteen. What it returns per pick is a *tier* - lead, major,
notable, brief - which is the page's own vocabulary, rather than a corrected 1-5
importance on a scale the summariser was given a rubric for (ADR 0030).

**It reads the day three times, shuffled.** The model answers with numbers from
a table, so its answer can depend on the order the table was in; that was
measured occasionally by `ainews eval rank-stability`, after publication, on an
order the reader had already been given. Three shuffled calls are aggregated by
Borda count and the bulletin ships with the number saying how much they agreed.
The cost is two extra rank calls - the rank prompt is the summaries, not the
articles, so this is cents on a run whose summarise step is dollars.

Its input is the *day*, not the run: every relevant summary in the window that
no earlier day's bulletin has already published. That is what makes a second
press re-rank rather than publish a two-story supplement.

If every call fails the bulletin still ships, ordered by importance and then
source weight, which is what a person would do with the same table. Such a
bulletin has `agreement` of `None` - there was no reading to agree with - and
that is how the page knows to say no editor stood behind it.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings, get_settings
from ainews.db import Article, Bulletin, BulletinItem, Source, Summary
from ainews.db.models import utcnow
from ainews.pipeline.agreement import RANK_PASSES, agreement, borda
from ainews.pipeline.llm import ranker, usage_from_message
from ainews.pipeline.prompts import load_prompt
from ainews.pipeline.state import TIER_ORDER, RankedDigest, Tier

log = logging.getLogger(__name__)

FALLBACK_NOTE = {
    "tr": "Sıralama modelsiz yapıldı: önem puanı, sonra kaynak ağırlığı.",
    "en": "Ranked without the model: importance score first, source weight second.",
}

# How many of yesterday's headlines the ranker is shown. Enough to recognise a
# thread it has already led with, short enough that it is context rather than a
# second candidate table.
PREVIOUS_HEADLINES = 8


@dataclass(slots=True)
class Candidate:
    """One story on offer, as the ranker is shown it."""

    summary_id: int
    article_id: int
    source: str
    weight: float
    title: str
    summary: str
    why_it_matters: str
    importance: int
    kind: str
    age_hours: float | None


@dataclass(slots=True)
class Ranking:
    """What the three calls produced, or what stood in for them.

    `order` is summary ids in reading order. `agreement` is `None` when fewer
    than two calls came back - one reading agrees with nothing, and a number
    invented for that case would be indistinguishable from a real one.
    """

    order: list[int] = field(default_factory=list)
    tiers: dict[int, Tier] = field(default_factory=dict)
    reasons: dict[int, str] = field(default_factory=dict)
    editor_note: str = ""
    agreement: float | None = None
    passes: list[list[int]] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass(slots=True)
class _Pass:
    """One rank call's answer."""

    order: list[int]
    tiers: dict[int, Tier]
    reasons: dict[int, str]
    editor_note: str
    tokens_in: int
    tokens_out: int


# -- what the ranker is given -------------------------------------------------


async def day_pool(
    session: AsyncSession, day: str, language: str, settings: Settings | None = None
) -> list[Candidate]:
    """Every summary today's bulletin may draw on.

    Not "the summaries this run wrote", which is what made a run and a bulletin
    the same object: candidate selection is a delta over unsummarised articles,
    so the second press of a day saw two stories and published a two-story
    front page (ADR 0030). The pool is the window, minus anything an *earlier
    day* already published - re-ranking today is the point, re-leading with
    yesterday's lead is not.

    Ordered heaviest source first and then earliest published, which is
    `dedupe`'s survivor order. It is a deterministic starting point rather than
    a meaningful one; the shuffles are what stop it being read as a hint.
    """
    settings = settings or get_settings()
    horizon = utcnow() - timedelta(days=settings.collect_max_age_days)
    published_elsewhere = (
        select(BulletinItem.summary_id)
        .join(Bulletin, Bulletin.id == BulletinItem.bulletin_id)
        .where(BulletinItem.summary_id == Summary.id)
        .where(Bulletin.day != day)
        .exists()
    )
    rows = (
        await session.execute(
            select(Summary, Article, Source.name, Source.weight)
            .join(Article, Article.id == Summary.article_id)
            .join(Source, Source.id == Article.source_id)
            .where(Summary.language == language)
            .where(Summary.relevant.is_(True))
            .where(Article.dup_of.is_(None))
            .where(Article.fetched_at >= horizon)
            .where(~published_elsewhere)
            .order_by(
                Source.weight.desc(),
                Article.published_at.asc().nullslast(),
                Article.id.asc(),
            )
        )
    ).all()

    now = utcnow()
    pool: list[Candidate] = []
    for summary, article, source_name, weight in rows:
        stamp = article.published_at or article.fetched_at
        pool.append(
            Candidate(
                summary_id=summary.id,
                article_id=article.id,
                source=source_name,
                weight=weight,
                title=summary.title_local,
                summary=summary.summary,
                why_it_matters=summary.why_it_matters,
                importance=summary.importance,
                kind=summary.kind,
                age_hours=(now - stamp).total_seconds() / 3600 if stamp else None,
            )
        )
    return pool


async def previous_headlines(
    session: AsyncSession, day: str, language: str, limit: int = PREVIOUS_HEADLINES
) -> list[str]:
    """What the last bulletin before today led with.

    Given to the ranker so it can tell a genuinely new development from the next
    article about the same week-old thread. It is context and not a filter: the
    pool already excludes anything an earlier day published, so nothing here can
    be picked again - the list exists to say "this was already the story", which
    is a reason to rank its follow-up lower rather than a reason to drop it.
    """
    latest = (
        await session.execute(
            select(Bulletin.id)
            .where(Bulletin.language == language)
            .where(Bulletin.day < day)
            .order_by(Bulletin.day.desc(), Bulletin.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        return []
    return list(
        (
            await session.execute(
                select(Summary.title_local)
                .join(BulletinItem, BulletinItem.summary_id == Summary.id)
                .where(BulletinItem.bulletin_id == latest)
                .order_by(BulletinItem.position)
                .limit(limit)
            )
        ).scalars()
    )


def _age(hours: float | None) -> str:
    if hours is None:
        return "age unknown"
    if hours < 1:
        return "under an hour old"
    if hours < 48:
        return f"{round(hours)}h old"
    return f"{round(hours / 24)}d old"


def build_candidate_table(candidates: list[Candidate]) -> str:
    """The numbered table the model answers about.

    `why_it_matters` is in it because that sentence is the summariser's own
    answer to the question the ranker is being asked, and leaving it out meant
    the ranker re-derived it from three sentences of what-happened. The age is
    in it because a day's bulletin drawn from a seven-day window otherwise has
    no way to tell this morning's release from last Tuesday's.
    """
    lines = []
    for number, item in enumerate(candidates, start=1):
        lines.append(
            f"{number}. [{item.source} · weight {item.weight:.1f} · {item.kind} · "
            f"{_age(item.age_hours)} · importance {item.importance}] {item.title}\n"
            f"   {item.summary}\n"
            f"   Why it matters: {item.why_it_matters}"
        )
    return "\n\n".join(lines)


def build_previous_block(headlines: list[str]) -> str:
    """Empty when there is no previous bulletin, so the prompt shows no heading
    with nothing under it - the shape that invites a model to fill it in."""
    if not headlines:
        return ""
    listed = "\n".join(f"- {headline}" for headline in headlines)
    return f"\n\nPREVIOUSLY PUBLISHED (context, not candidates):\n{listed}"


# -- one call -----------------------------------------------------------------


async def rank_once(
    candidates: list[Candidate],
    language: str,
    *,
    top_n: int,
    previous: list[str],
    settings: Settings | None = None,
    model: str | None = None,
) -> _Pass | None:
    """One rank call over one arrangement of the table. `None` if it failed.

    The caller shuffles; this function ranks what it is handed and maps the
    model's ordinals back onto summary ids, so a shuffled call and the
    production call are the same code path.
    """
    settings = settings or get_settings()
    prompt = load_prompt("rank", language).format(
        top_n=top_n,
        candidates=build_candidate_table(candidates),
        previous=build_previous_block(previous),
    )

    try:
        client = ranker(settings, model).with_structured_output(RankedDigest, include_raw=True)
        response = await client.ainvoke(prompt)
        parsed = response.get("parsed") if isinstance(response, dict) else None
        if parsed is None:
            raise ValueError("ranker returned unparsable output")
    except Exception as exc:
        log.warning("a rank call failed (%s)", exc)
        return None

    usage = usage_from_message(response.get("raw"))
    result = _Pass(
        order=[],
        tiers={},
        reasons={},
        editor_note=parsed.editor_note.strip(),
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
    )
    # The model answers with ordinals from the prompt, so anything out of range
    # or repeated is dropped rather than trusted.
    seen: set[int] = set()
    for pick in parsed.picks:
        index = pick.number - 1
        if not (0 <= index < len(candidates)) or index in seen:
            continue
        seen.add(index)
        summary_id = candidates[index].summary_id
        result.order.append(summary_id)
        result.tiers[summary_id] = pick.tier
        result.reasons[summary_id] = pick.reason.strip()
        if len(result.order) == top_n:
            break
    return result if result.order else None


# -- the three, aggregated ----------------------------------------------------


def _fallback(candidates: list[Candidate], top_n: int, language: str) -> Ranking:
    ordered = sorted(candidates, key=lambda c: (c.importance, c.weight), reverse=True)[:top_n]
    order = [c.summary_id for c in ordered]
    return Ranking(
        order=order,
        tiers={summary_id: _band(position) for position, summary_id in enumerate(order, start=1)},
        editor_note=FALLBACK_NOTE.get(language, ""),
    )


def _band(position: int) -> Tier:
    """A place in an order read back as the tier it would have been.

    Only for a bulletin no model ranked. The bands are the shape the page draws
    anyway - one lead, a few majors, the rest - so a fallback bulletin is
    legible rather than uniform, and `agreement is None` beside it is what says
    no editor chose them.
    """
    if position == 1:
        return "lead"
    if position <= 4:
        return "major"
    if position <= 9:
        return "notable"
    return "brief"


def _majority_tier(votes: list[Tier]) -> Tier:
    """The tier most readings gave a story, ties falling to the weaker one.

    Down and not up on a tie: two readings that split between lead and major
    have not agreed that this leads the day, and a page whose only ranking
    indicator is size should not draw an argument as a conclusion.
    """
    counted = Counter(votes)
    best = max(counted.values())
    return max((tier for tier in TIER_ORDER if counted.get(tier) == best), key=TIER_ORDER.index)


def _aggregate(passes: list[_Pass], top_n: int) -> Ranking:
    orders = [p.order for p in passes]
    order = borda(orders)[:top_n]

    tiers: dict[int, Tier] = {}
    reasons: dict[int, str] = {}
    for summary_id in order:
        tiers[summary_id] = _majority_tier(
            [p.tiers[summary_id] for p in passes if summary_id in p.tiers]
        )
        # The first non-empty reason in aggregate order. They are three answers
        # to one question, and concatenating them would put three sentences
        # under a story to explain a single placement.
        reasons[summary_id] = next(
            (p.reasons.get(summary_id, "") for p in passes if p.reasons.get(summary_id)), ""
        )

    # At most one lead, and it is the one the aggregate put first. Three
    # readings each allowed one lead can still name three different stories.
    leads = [summary_id for summary_id in order if tiers[summary_id] == "lead"]
    for summary_id in leads[1:]:
        tiers[summary_id] = "major"

    return Ranking(
        order=order,
        tiers=tiers,
        reasons=reasons,
        # The editor's note comes from the reading that agreed most with the
        # published order, so the prose in front of the reader describes the
        # list the reader is looking at.
        editor_note=max(passes, key=lambda p: _overlap(p.order, order)).editor_note,
        agreement=agreement(orders) if len(orders) > 1 else None,
        passes=orders,
        tokens_in=sum(p.tokens_in for p in passes),
        tokens_out=sum(p.tokens_out for p in passes),
    )


def _overlap(candidate: list[int], published: list[int]) -> int:
    return len(set(candidate) & set(published))


async def rank_day(
    candidates: list[Candidate],
    language: str,
    *,
    previous: list[str] | None = None,
    settings: Settings | None = None,
    model: str | None = None,
    seed: str = "",
    passes: int = RANK_PASSES,
) -> Ranking:
    """Read the day `passes` times over shuffled tables and aggregate.

    The first arrangement is the pool's own order and the rest are shuffled from
    `seed`, so a run is reproducible from its id and the production reading is
    one of the shuffles rather than a privileged call beside them.
    """
    if not candidates:
        return Ranking()
    settings = settings or get_settings()
    top_n = min(settings.digest_top_n, len(candidates))
    previous = previous or []

    arrangements = [list(candidates)]
    rng = random.Random(seed)
    for _ in range(max(0, passes - 1)):
        shuffled = list(candidates)
        rng.shuffle(shuffled)
        arrangements.append(shuffled)

    results = await asyncio.gather(
        *(
            rank_once(
                arrangement,
                language,
                top_n=top_n,
                previous=previous,
                settings=settings,
                model=model,
            )
            for arrangement in arrangements
        )
    )
    usable = [result for result in results if result is not None]
    if not usable:
        log.warning("every rank call failed; falling back to importance order")
        return _fallback(candidates, top_n, language)
    return _aggregate(usable, top_n)
