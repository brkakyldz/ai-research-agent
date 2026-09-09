"""Redoing one article's body and its summaries.

The operator's answer to two things. A reader marks a summary wrong on a fact,
and the fix is to give the model the article again rather than to argue with the
prompt. And a row whose body cannot be attributed - every article stored before
the article and the web context were separated (ADR 0029) - can be given one,
because the URL is still there.

Deliberately not a node and not on the graph. It touches one article at a time,
it is started by a person at a terminal, and the pipeline never calls it: a
repair that could run itself would be a second thing that spends money without a
press.

Tier 3 is never used here. The point is a body that can be attributed, and a web
search for the title is the one source that cannot be.

It rewrites text and cannot move a story. Where a story sits is a fact about a
bulletin (ADR 0030) and this touches summaries, so a repair to one article has
no way to re-order the day around it even if it wanted to - which is the shape
the old two-column design had to be told, in a comment, not to do.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings, get_settings
from ainews.db import Article, Source, Summary
from ainews.pipeline.nodes.summarize import draft_summary
from ainews.pipeline.pricing import resolve_model
from ainews.sources.extract import clean_html, fetch_article, is_usable

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RepairResult:
    article_id: int
    refetched: bool
    body_source: str | None
    summaries_rewritten: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


async def unattributed_articles(session: AsyncSession) -> list[int]:
    """Summarised articles whose body nothing can vouch for.

    `unknown` is what migration 0003 wrote onto every row that existed when the
    columns were added: their bodies may or may not carry search text appended
    with no marker, and there is no way to tell now. Everything written since
    says `feed` or `fetch`.
    """
    rows = (
        await session.execute(
            select(Article.id)
            .where(Article.body_source == "unknown")
            .where(select(Summary.id).where(Summary.article_id == Article.id).exists())
            .order_by(Article.id)
        )
    ).scalars()
    return list(rows)


async def refetch_body(session: AsyncSession, article: Article) -> bool:
    """Give the article a body with a provenance. True when one was found.

    Tier 2 only. Tier 1's input - the feed's own HTML - was overwritten in place
    the first time this article was enriched, so it cannot be replayed; tier 3
    is the thing being repaired.
    """
    body = await fetch_article(article.url)
    if not is_usable(body):
        return False
    article.body_text = clean_html(body) or body
    article.body_source = "fetch"
    article.extra_text = None
    await session.commit()
    return True


async def resummarize(
    session: AsyncSession,
    article_id: int,
    settings: Settings | None = None,
    *,
    model: str | None = None,
    refetch: bool = True,
) -> RepairResult:
    """Re-summarise one article in every language it already has a summary in.

    Every language, because a repair that fixed the Turkish bulletin and left
    the English one carrying the same wrong fact would be half a repair, and the
    two were written from the same body.
    """
    settings = settings or get_settings()
    model = resolve_model(model, settings.openai_model_summarize)

    article = await session.get(Article, article_id)
    if article is None:
        return RepairResult(article_id, False, None, 0, error="no such article")

    refetched = await refetch_body(session, article) if refetch else False
    if not article.body_text:
        return RepairResult(
            article_id, refetched, article.body_source, 0, error="no body to summarise from"
        )

    languages = list(
        (
            await session.execute(
                select(Summary.language).where(Summary.article_id == article_id).distinct()
            )
        ).scalars()
    )
    if not languages:
        return RepairResult(article_id, refetched, article.body_source, 0, error="no summaries")

    rewritten = 0
    for language in languages:
        # Through the call the pipeline uses, not a second implementation: a
        # repaired summary has to be the summary a run would have written.
        draft = await draft_summary(article_id, language, settings, model)
        if isinstance(draft, str):
            log.warning("resummarize failed for article %d (%s): %s", article_id, language, draft)
            continue
        rows = (
            await session.execute(
                select(Summary)
                .where(Summary.article_id == article_id)
                .where(Summary.language == language)
            )
        ).scalars()
        for row in rows:
            row.title_local = draft.parsed.title_local
            row.summary = draft.parsed.summary
            row.why_it_matters = draft.parsed.why_it_matters
            row.tags_json = json.dumps(draft.parsed.tags, ensure_ascii=False)
            row.importance = draft.parsed.importance
            row.relevant = draft.parsed.relevant
            row.kind = draft.parsed.kind
            # The row's cost is what its text cost, which is now this call. The
            # run that first paid for it still carries what it paid on its own
            # row; the same dollar has not moved, a second one was spent.
            row.model = draft.model
            row.tokens_in = draft.tokens_in
            row.tokens_out = draft.tokens_out
            row.est_cost_usd = draft.est_cost_usd
            rewritten += 1
    await session.commit()

    return RepairResult(article_id, refetched, article.body_source, rewritten)


async def source_of(session: AsyncSession, article_id: int) -> str:
    article = await session.get(Article, article_id)
    if article is None:
        return "unknown"
    source = await session.get(Source, article.source_id) if article else None
    return source.name if source else "unknown"
