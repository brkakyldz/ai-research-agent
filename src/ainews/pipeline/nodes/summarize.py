"""The summarize step: one structured LLM call per article.

This is the node the `Send` fan-out lands in, so it runs once per candidate, in
parallel, each invocation independent of the others. Three consequences shape it.

It reads its article from the database and writes its summary back there, in
its own transaction, before returning. A summary is a property of an article and
not of the press that paid for it (ADR 0030), so there is nothing to wait for: a
run that dies at `rank` has still bought ninety summaries and they are all in the
archive, where the next press will rank them instead of buying them again.

It returns a list of one - an id and a token count, no text - because the state
field it writes into has an `operator.add` reducer, which is how a hundred
parallel branches write to one key without overwriting each other.

And it never raises. A branch that throws would fail the whole graph, so a failed
article becomes an entry in `errors` and ninety-nine summaries still reach the
digest.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ainews.config import Settings, get_settings
from ainews.db import Article, Source, Summary
from ainews.db.session import session_scope
from ainews.pipeline.llm import summarizer, usage_from_message
from ainews.pipeline.pricing import estimate_cost, resolve_model
from ainews.pipeline.prompts import load_prompt, tag_vocabulary_line
from ainews.pipeline.state import ArticleSummary, PipelineState, SummarizeTask

log = logging.getLogger(__name__)

# The model's context is over a million tokens, but a news summary does not need
# a whole page and a longer prompt is a slower, dearer one.
MAX_BODY_CHARS = 5000
# Web context is a fallback for an article that arrived nearly empty, so it gets
# a smaller share than the article itself.
MAX_EXTRA_CHARS = 2000

# The line that separates the article from text found by searching for its
# title. One constant, in English in both prompts, because it is a delimiter
# rather than prose: each prompt file explains in its own language what the
# marker means, and neither has to keep a translation of it in step.
WEB_CONTEXT_MARKER = "--- WEB CONTEXT: NOT THE ARTICLE ---"


def build_prompt(
    language: str,
    *,
    source: str,
    title: str,
    url: str,
    published: str,
    body: str,
    extra: str = "",
) -> str:
    template = load_prompt("summarize", language)
    return template.format(
        source=source,
        title=title,
        url=url,
        published=published,
        body=(body or "(no body text available - work from the title alone)")[:MAX_BODY_CHARS],
        # Empty when there is none, so the ordinary call carries no marker and
        # no empty heading. A prompt that announces a section and then shows
        # nothing invites the model to fill it in.
        extra=(
            f"\n\n{WEB_CONTEXT_MARKER}\n{extra[:MAX_EXTRA_CHARS]}" if (extra or "").strip() else ""
        ),
        # The preferred tags, formatted in rather than written in the file, so
        # the list the model is shown is the list `evals.checks` measures against.
        tags=tag_vocabulary_line(),
    )


@dataclass(slots=True)
class Draft:
    """One paid call's answer, before anything is written down.

    The seam between "ask the model" and "put it in the archive", because two
    callers need the first half and disagree about the second: the node inserts
    a row, `pipeline/repair.resummarize` overwrites one. Without this they were
    the node and a second implementation of the node, and a repaired summary has
    to be the summary a run would have written.
    """

    parsed: ArticleSummary
    model: str
    tokens_in: int
    tokens_out: int
    est_cost_usd: float


async def draft_summary(
    article_id: int,
    language: str,
    settings: Settings | None = None,
    model: str | None = None,
) -> Draft | str:
    """Summarise one article, or say in one sentence why there is no summary.

    A string and not an exception: every caller has to carry on without this
    article, and a return value makes that the ordinary path rather than a
    `try` around each one.
    """
    settings = settings or get_settings()

    async with session_scope() as session:
        article = await session.get(Article, article_id)
        if article is None:
            return f"article {article_id} vanished before summarising"
        source = await session.get(Source, article.source_id)
        prompt = build_prompt(
            language,
            source=source.name if source else "unknown",
            title=article.title,
            url=article.url,
            published=article.published_at.isoformat() if article.published_at else "unknown",
            body=article.body_text or "",
            extra=article.extra_text or "",
        )

    model_name = resolve_model(model, settings.openai_model_summarize)
    try:
        client = summarizer(settings, model_name).with_structured_output(
            ArticleSummary, include_raw=True
        )
        response = await client.ainvoke(prompt)
    except Exception as exc:
        log.warning("summarise failed for article %d: %s", article_id, exc)
        return f"article {article_id}: {type(exc).__name__}: {exc}"[:300]

    parsed = response.get("parsed") if isinstance(response, dict) else None
    if parsed is None:
        detail = response.get("parsing_error") if isinstance(response, dict) else "no parsed output"
        log.warning("summarise returned unusable output for article %d: %s", article_id, detail)
        return f"article {article_id}: unparsable model output"

    usage = usage_from_message(response.get("raw"))
    return Draft(
        parsed=parsed,
        model=model_name,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        est_cost_usd=estimate_cost(model_name, usage.tokens_in, usage.tokens_out),
    )


async def summarize_article(
    state: SummarizeTask, settings: Settings | None = None
) -> PipelineState:
    """Summarise one article and commit it. Never raises; failures land in `errors`."""
    article_id = state["article_id"]
    language = state["language"]

    draft = await draft_summary(article_id, language, settings, state.get("model"))
    if isinstance(draft, str):
        return {"errors": [draft]}

    async with session_scope() as session:
        summary = Summary(
            article_id=article_id,
            language=language,
            title_local=draft.parsed.title_local,
            summary=draft.parsed.summary,
            why_it_matters=draft.parsed.why_it_matters,
            tags_json=json.dumps(draft.parsed.tags, ensure_ascii=False),
            importance=draft.parsed.importance,
            key_fact=draft.parsed.key_fact.strip() or None,
            relevant=draft.parsed.relevant,
            kind=draft.parsed.kind,
            model=draft.model,
            tokens_in=draft.tokens_in,
            tokens_out=draft.tokens_out,
            est_cost_usd=draft.est_cost_usd,
        )
        session.add(summary)
        try:
            await session.commit()
        except IntegrityError:
            # `uq_summary_article_lang`. `dedupe` selects articles carrying no
            # summary at all, so the only way here is two presses overlapping on
            # one article - and the row already committed is as good as the one
            # just paid for. The tokens are still reported, because they were
            # still spent.
            await session.rollback()
            existing = (
                await session.execute(
                    select(Summary.id)
                    .where(Summary.article_id == article_id)
                    .where(Summary.language == language)
                )
            ).scalar_one_or_none()
            if existing is None:
                raise
            summary_id = existing
        else:
            summary_id = summary.id

    return {
        "summaries": [
            {
                "summary_id": summary_id,
                "article_id": article_id,
                "tokens_in": draft.tokens_in,
                "tokens_out": draft.tokens_out,
                "est_cost_usd": draft.est_cost_usd,
            }
        ]
    }
