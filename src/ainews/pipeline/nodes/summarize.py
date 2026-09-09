"""The summarize step: one structured LLM call per article.

This is the node the `Send` fan-out lands in, so it runs once per candidate, in
parallel, each invocation independent of the others. Three consequences shape it.

It reads its article from the database rather than from the state, because state
is checkpointed on every superstep and a hundred article bodies in there would
make each checkpoint enormous.

It returns a list of one, because the state field it writes into has an
`operator.add` reducer - that is how a hundred parallel branches write to one key
without overwriting each other.

And it never raises. A branch that throws would fail the whole graph, so a failed
article becomes an entry in `errors` and ninety-nine summaries still reach the
digest.
"""

from __future__ import annotations

import logging

from ainews.config import Settings, get_settings
from ainews.db import Article, Source
from ainews.db.session import session_scope
from ainews.pipeline.llm import summarizer, usage_from_message
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


async def summarize_article(
    state: SummarizeTask, settings: Settings | None = None
) -> PipelineState:
    """Summarise one article. Never raises; failures land in `errors`."""
    settings = settings or get_settings()
    article_id = state["article_id"]
    language = state["language"]

    async with session_scope() as session:
        article = await session.get(Article, article_id)
        if article is None:
            return {"errors": [f"article {article_id} vanished before summarising"]}
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

    try:
        model = summarizer(settings, state.get("model")).with_structured_output(
            ArticleSummary, include_raw=True
        )
        response = await model.ainvoke(prompt)
    except Exception as exc:
        log.warning("summarise failed for article %d: %s", article_id, exc)
        return {"errors": [f"article {article_id}: {type(exc).__name__}: {exc}"[:300]]}

    parsed = response.get("parsed") if isinstance(response, dict) else None
    if parsed is None:
        detail = response.get("parsing_error") if isinstance(response, dict) else "no parsed output"
        log.warning("summarise returned unusable output for article %d: %s", article_id, detail)
        return {"errors": [f"article {article_id}: unparsable model output"]}

    usage = usage_from_message(response.get("raw"))
    return {
        "summaries": [
            {
                "article_id": article_id,
                "title_local": parsed.title_local,
                "summary": parsed.summary,
                "why_it_matters": parsed.why_it_matters,
                "tags": parsed.tags,
                "importance": parsed.importance,
                "tokens_in": usage.tokens_in,
                "tokens_out": usage.tokens_out,
            }
        ]
    }
