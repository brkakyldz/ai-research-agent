"""The persist step: write the summaries and close out the run row.

Everything before this point is in graph state, which is machine state with its
own lifecycle. This is where a run becomes something the dashboard can read a
week later.

Cost is computed here rather than reported by the API, from the token counts each
LLM node carried back on its payload. It is an estimate and the run row says so -
prompt caching in particular makes the real bill lower than this number.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings, get_settings
from ainews.db import Run, Summary
from ainews.db.models import utcnow
from ainews.pipeline.llm import estimate_cost
from ainews.pipeline.state import PipelineState, SummaryPayload

log = logging.getLogger(__name__)

# The rank node reports its tokens on a payload with this id rather than opening
# a second state channel just for two integers.
TOKEN_CARRIER_ID = -1

# Errors are shown in /runs verbatim, so the column has to stay readable.
MAX_STORED_ERRORS = 20


async def persist_run(
    session: AsyncSession, state: PipelineState, settings: Settings | None = None
) -> Run:
    settings = settings or get_settings()
    run = await session.get(Run, state["run_id"])
    if run is None:
        raise RuntimeError(f"run {state['run_id']} disappeared mid-flight")

    payloads = [s for s in (state.get("summaries") or []) if s["article_id"] != TOKEN_CARRIER_ID]
    ranks = {item["article_id"]: item["rank"] for item in (state.get("ranked") or [])}

    # A retried `Send` - a resumed run, a superstep replayed - appends its payload
    # a second time, because `summaries` is a concatenating reducer. The unique
    # index on (article_id, run_id, language) would then abort this whole commit
    # and lose every summary in the run, so collapse to one row per article.
    by_article: dict[int, SummaryPayload] = {}
    for payload in payloads:
        by_article[payload["article_id"]] = payload

    for payload in by_article.values():
        session.add(
            Summary(
                article_id=payload["article_id"],
                run_id=run.id,
                language=state["language"],
                title_local=payload["title_local"],
                summary=payload["summary"],
                why_it_matters=payload["why_it_matters"],
                tags_json=json.dumps(payload["tags"], ensure_ascii=False),
                importance=payload["importance"],
                rank=ranks.get(payload["article_id"]),
            )
        )

    # The rank call rides on the carrier payload and is priced at *its* model.
    # Until 2026-09-06 every token was costed at `openai_model_summarize`, which
    # is only right while the two knobs point at the same model - the day the
    # summariser moves up to terra (ADR 0001) the run row would have lied.
    carrier = [s for s in (state.get("summaries") or []) if s["article_id"] == TOKEN_CARRIER_ID]
    rank_in = sum(s["tokens_in"] for s in carrier)
    rank_out = sum(s["tokens_out"] for s in carrier)
    summarize_in = sum(s["tokens_in"] for s in payloads)
    summarize_out = sum(s["tokens_out"] for s in payloads)
    tokens_in = summarize_in + rank_in
    tokens_out = summarize_out + rank_out
    errors = state.get("errors") or []

    run.finished_at = utcnow()
    run.n_collected = state.get("n_collected", 0)
    run.n_new = state.get("n_new", 0)
    run.n_summarized = len(by_article)
    run.tokens_in = tokens_in
    run.tokens_out = tokens_out
    run.est_cost_usd = estimate_cost(
        settings.openai_model_summarize, summarize_in, summarize_out
    ) + estimate_cost(settings.openai_model, rank_in, rank_out)
    run.editor_note = state.get("editor_note") or None
    run.error = "\n".join(errors[:MAX_STORED_ERRORS]) or None
    # "partial" is a real outcome, not a failure: a feed 404ed or three articles
    # would not summarise, and the other ninety-seven are still a digest.
    run.status = "partial" if errors else "ok"

    await session.commit()
    log.info(
        "run %s finished: %d summaries, %d/%d tokens, ~$%.4f, status=%s",
        run.id,
        run.n_summarized,
        tokens_in,
        tokens_out,
        run.est_cost_usd,
        run.status,
    )
    return run
