"""The digest graph.

    START -> collect -> dedupe -> enrich -> [Send x N] summarize -> rank -> persist -> END

Only one edge in that line is interesting: the one into `summarize`. It is a
conditional edge returning a list of `Send` objects, one per surviving candidate,
which is LangGraph's map-reduce - a hundred independent branches in a single
superstep, each writing into a state key with an `operator.add` reducer.

Two settings on the invoke matter. A hundred simultaneous requests will collect
429s, so `max_concurrency` throttles them to `SUMMARIZE_BATCH_SIZE` at a time.
And `recursion_limit` counts supersteps: a `Send` fan-out is one superstep
however wide it is, so the real depth here is six and the default of 25 would
do - the runner raises it anyway as insurance against a future node that loops.

There is no checkpointer. A checkpoint is a way of not losing work when a run
dies, and the work this run does is now durable as it happens: each branch
commits its summary before it returns (ADR 0030). A run that dies at `rank` has
bought ninety summaries and they are all in the archive, so the recovery is to
press again - the next press ranks them instead of buying them again, which is
cheaper and simpler than replaying a graph from a checkpoint whose prompts,
models and schema have all moved since it was written (ADR 0032).
"""

from __future__ import annotations

import logging
from functools import lru_cache

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ainews.config import get_settings
from ainews.db.session import session_scope
from ainews.pipeline.nodes.collect import collect_articles
from ainews.pipeline.nodes.dedupe import dedupe_candidates
from ainews.pipeline.nodes.enrich import enrich_articles
from ainews.pipeline.nodes.persist import persist_run
from ainews.pipeline.nodes.rank import day_pool, previous_headlines, rank_day
from ainews.pipeline.nodes.summarize import summarize_article
from ainews.pipeline.pricing import CostCeiling, estimate_digest_cost
from ainews.pipeline.state import PipelineState
from ainews.pipeline.steps import record_fan_out, step

log = logging.getLogger(__name__)


# Every adapter below is wrapped in `step()`, which times it and writes one
# `run_steps` row (ADR 0022). The counts each one reports are its own - "in" and
# "out" mean different things at `collect` and at `rank` - so each states them
# rather than a wrapper inferring them from the state it can see.


async def collect_node(state: PipelineState) -> PipelineState:
    async with step(state["run_id"], "collect") as s:
        async with session_scope() as session:
            stats = await collect_articles(session)
        s.counts(stats.n_seen, stats.n_new)
        s.note_key("collect", sources=stats.n_sources, unchanged=stats.n_not_modified)
        if stats.errors:
            s.status = "partial"
            s.note("; ".join(stats.errors))
        # A dead feed marks this step partial and stops there. It used to be
        # copied into `state["errors"]`, which `persist_run` reads to decide the
        # whole run's status - so one rotting feed made every bulletin for the
        # next five polls `partial`, and the word stopped telling the reader
        # anything. `partial` on a digest means candidates went unsummarised;
        # which feeds answered is a property of the poll, and it is on the step
        # row above, where the run detail page already reads it.
        return {"n_collected": stats.n_seen, "n_new": stats.n_new}


async def dedupe_node(state: PipelineState) -> PipelineState:
    async with step(state["run_id"], "dedupe") as s:
        async with session_scope() as session:
            candidate_ids, stats = await dedupe_candidates(session)
        s.counts(stats.n_candidates, len(candidate_ids))
        s.note_key("dedupe", dropped=stats.n_duplicates)
        _guard_cost(state, len(candidate_ids))
        return {"candidate_ids": candidate_ids}


def _guard_cost(state: PipelineState, n_candidates: int) -> None:
    """Refuse a press that would cost more than the ceiling, before it spends.

    Here and not at the press, because here is the first moment the real number
    is known. `/runs` counts candidates to write the question, but a fresh
    installation has collected nothing when the button is first pressed: the
    count is zero, `collect` then brings in a week of backlog, and the press
    that most needs a ceiling is the one a pre-flight check waves through.

    After `dedupe` and before `enrich` is also the last free moment. Enrichment
    spends Tavily credits and the fan-out spends tokens; everything up to here
    is feed polling and string comparison.
    """
    settings = get_settings()
    ceiling = settings.digest_max_cost_usd
    if not ceiling or not n_candidates:
        return
    estimate = estimate_digest_cost(
        n_candidates,
        state.get("model_summarize") or settings.openai_model_summarize,
        state.get("model_rank") or settings.openai_model,
    )
    if estimate > ceiling:
        raise CostCeiling(estimate, ceiling, n_candidates)


async def enrich_node(state: PipelineState) -> PipelineState:
    candidate_ids = state.get("candidate_ids") or []
    async with step(state["run_id"], "enrich") as s:
        if not candidate_ids:
            s.counts(0, 0)
            return {}
        async with session_scope() as session:
            stats = await enrich_articles(session, candidate_ids)
        # Out is "how many go to the model with a body", not "how many were
        # touched": an article the fetcher and Tavily both failed on is still
        # summarised, from its title alone, and that is the number worth seeing.
        s.counts(stats.n_examined, stats.n_examined - stats.n_still_empty)
        # Tavily is the only paid call in the run that costs credits rather than
        # tokens, so it is a note here rather than a number in the cost column.
        s.note_key("enrich", fetched=stats.n_fetched, tavily=stats.n_tavily)
        return {}


def fan_out_summaries(state: PipelineState) -> list[Send] | str:
    """One `Send` per candidate, or a jump to the end when there is nothing to do."""
    candidate_ids = state.get("candidate_ids") or []
    if not candidate_ids:
        return "persist"
    # The model rides on every `Send` rather than being read inside the node:
    # the branches are the run, and a run has to be summarised by one model even
    # if the environment changes halfway through it.
    model = state.get("model_summarize")
    return [
        Send(
            "summarize",
            {
                "run_id": state["run_id"],
                "language": state["language"],
                "article_id": article_id,
                "model": model,
            },
        )
        for article_id in candidate_ids
    ]


async def rank_node(state: PipelineState) -> PipelineState:
    """Rank the *day*, not the run.

    The pool is every relevant summary in the collection window that no earlier
    day's bulletin has published, so a second press re-ranks what is already
    there together with what it just bought (ADR 0030). That is why this node
    reads the database rather than `state["summaries"]`: the summaries this run
    wrote are a delta, and a delta is not a day. It also means the tie-break
    material - each story's source and weight - comes back in the query that
    selects the candidates, instead of a second join to name what the payloads
    could not carry.
    """
    async with step(state["run_id"], "rank") as s:
        async with session_scope() as session:
            candidates = await day_pool(session, state["day"], state["language"])
            previous = await previous_headlines(session, state["day"], state["language"])

        if not candidates:
            s.counts(0, 0)
            return {"ranked": [], "editor_note": ""}

        ranking = await rank_day(
            candidates,
            state["language"],
            previous=previous,
            model=state.get("model_rank"),
            seed=state["run_id"],
        )
        s.counts(len(candidates), len(ranking.order))
        # `persist_run`'s fallback, not an empty string: see the note in
        # `steps.record_fan_out`. The two have to price the same tokens the
        # same way.
        s.spend(
            state.get("model_rank") or get_settings().openai_model,
            ranking.tokens_in,
            ranking.tokens_out,
        )
        if ranking.agreement is None:
            s.note_key("rank_unranked")
        else:
            s.note_key(
                "rank_agreement",
                agreement=round(ranking.agreement * 100),
                passes=len(ranking.passes),
            )
        return {
            "ranked": [
                {
                    "summary_id": summary_id,
                    "position": position,
                    "tier": ranking.tiers[summary_id],
                    "reason": ranking.reasons.get(summary_id, ""),
                }
                for position, summary_id in enumerate(ranking.order, start=1)
            ],
            "editor_note": ranking.editor_note,
            "agreement": ranking.agreement,
            "rank_usage": {"tokens_in": ranking.tokens_in, "tokens_out": ranking.tokens_out},
        }


async def persist_node(state: PipelineState) -> PipelineState:
    async with step(state["run_id"], "persist") as s:
        # The fan-out's own row, written here because this is the first moment
        # both of its neighbours have rows to be measured between.
        await record_fan_out(state)
        async with session_scope() as session:
            run = await persist_run(session, state)
        s.counts(len(state.get("summaries") or []), run.n_summarized)
        return {}


@lru_cache(maxsize=1)
def build_graph() -> StateGraph:
    """The graph itself, built once.

    Cached because it is built from constants - six nodes and their edges, no
    settings and no session - so there is nothing per-run for it to close over.
    """
    graph = StateGraph(PipelineState)
    graph.add_node("collect", collect_node)
    graph.add_node("dedupe", dedupe_node)
    graph.add_node("enrich", enrich_node)
    graph.add_node("summarize", summarize_article)
    graph.add_node("rank", rank_node)
    graph.add_node("persist", persist_node)

    graph.add_edge(START, "collect")
    graph.add_edge("collect", "dedupe")
    graph.add_edge("dedupe", "enrich")
    graph.add_conditional_edges("enrich", fan_out_summaries, ["summarize", "persist"])
    graph.add_edge("summarize", "rank")
    graph.add_edge("rank", "persist")
    graph.add_edge("persist", END)
    return graph
