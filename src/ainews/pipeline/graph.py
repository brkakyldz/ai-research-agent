"""The digest graph.

    START -> collect -> dedupe -> enrich -> [Send x N] summarize -> rank -> persist -> END

Only one edge in that line is interesting: the one into `summarize`. It is a
conditional edge returning a list of `Send` objects, one per surviving candidate,
which is LangGraph's map-reduce - a hundred independent branches in a single
superstep, each writing into a state key with an `operator.add` reducer.

Two limits have to be raised for that to work at all. `recursion_limit` counts
supersteps and defaults to 25, which a hundred-item fan-out blows through, so the
runner raises it. And a hundred simultaneous requests will collect 429s, so
`max_concurrency` throttles them to `SUMMARIZE_BATCH_SIZE` at a time.

Checkpoints go to their own SQLite file, not the application database. They are
machine state with a different lifecycle: deleting them costs nothing, while
deleting `app.db` costs the archive.
"""

from __future__ import annotations

import logging
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ainews.config import Settings, get_settings
from ainews.db import Article, Source
from ainews.db.session import session_scope
from ainews.pipeline.nodes.collect import collect_articles
from ainews.pipeline.nodes.dedupe import dedupe_candidates
from ainews.pipeline.nodes.enrich import enrich_articles
from ainews.pipeline.nodes.persist import persist_run
from ainews.pipeline.nodes.rank import rank_summaries
from ainews.pipeline.nodes.summarize import summarize_article
from ainews.pipeline.state import PipelineState

log = logging.getLogger(__name__)


def checkpoint_path(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.sqlite_path.parent / "checkpoints.db"


async def collect_node(state: PipelineState) -> PipelineState:
    async with session_scope() as session:
        stats = await collect_articles(session)
    return {
        "n_collected": stats.n_seen,
        "n_new": stats.n_new,
        "errors": list(stats.errors or []),
    }


async def dedupe_node(state: PipelineState) -> PipelineState:
    async with session_scope() as session:
        candidate_ids, _ = await dedupe_candidates(session)
    return {"candidate_ids": candidate_ids}


async def enrich_node(state: PipelineState) -> PipelineState:
    candidate_ids = state.get("candidate_ids") or []
    if not candidate_ids:
        return {}
    async with session_scope() as session:
        await enrich_articles(session, candidate_ids)
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
    summaries = state.get("summaries") or []
    if not summaries:
        return {"ranked": [], "editor_note": ""}

    async with session_scope() as session:
        meta: dict[int, tuple[str, float]] = {}
        for item in summaries:
            article = await session.get(Article, item["article_id"])
            source = await session.get(Source, article.source_id) if article else None
            meta[item["article_id"]] = (
                source.name if source else "unknown",
                source.weight if source else 1.0,
            )

    ordered, note, tokens_in, tokens_out = await rank_summaries(
        summaries, meta, state["language"], model=state.get("model_rank")
    )
    return {
        "ranked": [{"article_id": aid, "rank": i} for i, aid in enumerate(ordered, start=1)],
        "editor_note": note,
        # The ranking call's tokens ride on a synthetic entry so `persist` can
        # add them to the run's total without a second channel.
        "summaries": [
            {
                "article_id": -1,
                "title_local": "",
                "summary": "",
                "why_it_matters": "",
                "tags": [],
                "importance": 3,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
            }
        ],
    }


async def persist_node(state: PipelineState) -> PipelineState:
    async with session_scope() as session:
        await persist_run(session, state)
    return {}


def build_graph() -> StateGraph:
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
