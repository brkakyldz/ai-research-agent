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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings, get_settings
from ainews.db import Run, Summary
from ainews.db.models import utcnow
from ainews.pipeline.pricing import estimate_cost
from ainews.pipeline.state import PipelineState, RankedItem, SummaryPayload

log = logging.getLogger(__name__)

# Errors are shown in /runs verbatim, so the column has to stay readable.
MAX_STORED_ERRORS = 20


async def persist_run(
    session: AsyncSession, state: PipelineState, settings: Settings | None = None
) -> Run:
    settings = settings or get_settings()
    run = await session.get(Run, state["run_id"])
    if run is None:
        raise RuntimeError(f"run {state['run_id']} disappeared mid-flight")

    payloads = state.get("summaries") or []
    ranked: dict[int, RankedItem] = {
        item["article_id"]: item for item in (state.get("ranked") or [])
    }

    # A retried `Send` - a resumed run, a superstep replayed - appends its payload
    # a second time, because `summaries` is a concatenating reducer. The unique
    # index on (article_id, run_id, language) would then abort this whole commit
    # and lose every summary in the run, so collapse to one row per article.
    by_article: dict[int, SummaryPayload] = {}
    for payload in payloads:
        by_article[payload["article_id"]] = payload

    # A run resumed after this node had already committed - which can happen
    # when the failure was in the bookkeeping after the commit - must not try
    # to insert the same rows again for the same reason.
    already = set(
        (
            await session.execute(
                select(Summary.article_id)
                .where(Summary.run_id == run.id)
                .where(Summary.language == state["language"])
            )
        ).scalars()
    )

    for payload in by_article.values():
        if payload["article_id"] in already:
            continue
        pick = ranked.get(payload["article_id"])
        session.add(
            Summary(
                article_id=payload["article_id"],
                run_id=run.id,
                language=state["language"],
                title_local=payload["title_local"],
                summary=payload["summary"],
                why_it_matters=payload["why_it_matters"],
                tags_json=json.dumps(payload["tags"], ensure_ascii=False),
                # Two scores, kept apart on purpose (ADR 0025): the summariser's
                # own, and the ranker's read of the same story against the whole
                # day. The page draws the second where there is one; the
                # evaluation layer compares the two.
                importance=payload["importance"],
                editor_importance=pick["importance"] if pick else None,
                rank=pick["rank"] if pick else None,
            )
        )

    # The rank call's tokens come on their own channel and are priced at *its*
    # model. Until 2026-09-06 every token was costed at `openai_model_summarize`,
    # which is only right while the two knobs point at the same model - the day
    # the summariser moves up to terra (ADR 0001) the run row would have lied.
    usage = state.get("rank_usage") or {"tokens_in": 0, "tokens_out": 0}
    rank_in, rank_out = usage["tokens_in"], usage["tokens_out"]
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
    # Priced at the models this run actually used, which since ADR 0020 is a
    # choice made at the press and may be nothing like the environment's
    # default. Reading `settings` here would have re-introduced the bug fixed
    # above in a worse form: the two knobs would agree with each other and both
    # disagree with the run.
    model_summarize = state.get("model_summarize") or settings.openai_model_summarize
    model_rank = state.get("model_rank") or settings.openai_model
    run.est_cost_usd = estimate_cost(model_summarize, summarize_in, summarize_out) + estimate_cost(
        model_rank, rank_in, rank_out
    )
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
