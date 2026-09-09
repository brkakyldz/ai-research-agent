"""One published day, as the plain dicts every check reads.

`checks.py` takes dicts and returns numbers, with no I/O in it at all - that is
what lets the same functions run over a recorded fixture inside `pytest` and
over the live database at the end of a press. This module is the one place that
turns rows into those dicts, so the fixture on disk and the numbers stored on a
bulletin are the same shape by construction rather than by two people
remembering.

Article bodies never leave here. What a check needs from a body is its numerals
and its capitalised tokens, and the fixture is committed to a public repository
(ADR 0019 §3): the body itself is someone else's text.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.clock import day_bounds
from ainews.db import Article, Bulletin, BulletinItem, Source, Summary

# The summariser's own cut-off, imported rather than restated: the numerals
# are taken from the text the model was shown, so a figure past the cut-off
# is one it could not have read and a summary carrying it is ungrounded.
from ainews.pipeline.nodes.summarize import MAX_BODY_CHARS
from ainews.quality.checks import (
    Story,
    content_floor,
    editor_note_shape,
    importance_distribution,
    numeral_values,
    ranked_order,
    ranker_vs_fallback,
    tag_vocabulary,
    tier_shape,
    ungrounded_numerals,
    unrepresented_fives,
    word_budget,
)

# A capitalised token: a word starting with an uppercase letter in either
# language. Enough for a name-grounding check; capped so a long body does not
# swell the fixture - 200 per story made the first one 170 KB.
_CAPITALISED = re.compile(r"\b[A-ZÇĞİÖŞÜ][\w\-']{1,}", re.UNICODE)
MAX_CAPITALISED = 80


def capitalised_tokens(body: str | None) -> list[str]:
    tokens = sorted({m.group(0) for m in _CAPITALISED.finditer(body or "")})
    return tokens[:MAX_CAPITALISED]


async def day_stories(session: AsyncSession, bulletin: Bulletin) -> list[Story]:
    """The bulletin's own items, and everything else its day summarised.

    Both, because half of what the checks measure - the tag vocabulary, the
    importance distribution, the free ordering - is about the summariser rather
    than about the editor, and measuring only the stories the editor kept would
    flatter it.

    The published ones come first, in the editor's order; the rest follow by the
    summariser's own score. That is the order a reader meets them in, and the
    order `ranked_order` reads.
    """
    start, end = day_bounds(bulletin.day)
    rows = (
        await session.execute(
            select(Summary, Article, Source, BulletinItem)
            .join(Article, Article.id == Summary.article_id)
            .join(Source, Source.id == Article.source_id)
            .outerjoin(
                BulletinItem,
                (BulletinItem.summary_id == Summary.id) & (BulletinItem.bulletin_id == bulletin.id),
            )
            .where(Summary.language == bulletin.language)
            .where(
                BulletinItem.id.isnot(None)
                | ((Summary.created_at >= start) & (Summary.created_at < end))
            )
            .order_by(
                BulletinItem.position.asc().nullslast(),
                Summary.importance.desc(),
                Summary.id.asc(),
            )
        )
    ).all()

    stories: list[Story] = []
    for summary, article, source, item in rows:
        try:
            tags = json.loads(summary.tags_json or "[]")
        except json.JSONDecodeError:
            tags = []
        seen = (article.body_text or "")[:MAX_BODY_CHARS]
        stories.append(
            {
                "article_id": article.id,
                "source": source.name,
                "weight": source.weight,
                "title": article.title,
                "title_local": summary.title_local,
                "summary": summary.summary,
                "why_it_matters": summary.why_it_matters,
                "tags": tags,
                # The summariser's own score, given with one article in view.
                # The checks read it for the free ordering and the distribution.
                "importance": summary.importance,
                # The one concrete thing the summariser said made this news.
                # `content_floor` asks whether it survived into the prose.
                "key_fact": summary.key_fact,
                "relevant": summary.relevant,
                "kind": summary.kind,
                # The editor's placement, or null for a story left out. Two
                # fields where there were two scores, and neither is the other's
                # fallback (ADR 0030).
                "position": item.position if item else None,
                "tier": item.tier if item else None,
                "reason": item.reason if item else None,
                "body_numerals": sorted(numeral_values(seen)),
                "body_capitalised": capitalised_tokens(seen),
                # Where the text those two were taken from came from. `unknown`
                # is every row written before the article and the web context
                # were separated (ADR 0029): its numerals may include figures
                # from a search result rather than from the article, so a
                # grounding floor computed over it is an upper bound.
                "body_source": article.body_source,
            }
        )
    return stories


def published_checks(stories: list[Story], editor_note: str | None) -> dict[str, Any]:
    """Every free check over one day, as the JSON a bulletin carries.

    Stored at publish rather than recomputed on demand, for the reason
    `prompt_version` exists: a number is a claim about the code and the prompts
    that produced it, and both move. A bulletin from last week re-scored by
    this week's checks is a measurement of neither week.
    """
    return {
        "n": len(stories),
        "n_published": len(ranked_order(stories)),
        "budget": word_budget(stories),
        "content": content_floor(stories),
        "ungrounded": [
            {"article_id": aid, "numerals": nums} for aid, nums in ungrounded_numerals(stories)
        ],
        "tags": tag_vocabulary(stories),
        "importance": importance_distribution(stories),
        "overlap": ranker_vs_fallback(stories),
        "tiers": tier_shape(stories),
        "unrepresented": unrepresented_fives(stories),
        "editor_note": editor_note_shape(editor_note),
    }
