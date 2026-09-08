"""What each node of the graph did, written down as it happens.

The `runs` row has always said what a run cost in total. It never said where
that went, so the two questions a person actually asks of a slow or expensive
run - which node ate the two minutes, which node ate the eleven cents - had no
answer short of reading the log. `run_steps` answers both, and `/runs/<id>`
reads it (ADR 0022).

Four properties are deliberate.

**The nodes are untouched.** Every call into this module is in `graph.py`, in the
thin adapters that already stand between the graph and the node functions, or in
`runner.py` for the collect poll, which never enters the graph at all. Nothing in
`nodes/` knows it is being timed, which is the same line ADR 0019 draws around
the evaluation layer and the same one `observability.py` draws around Phoenix.

**The counts are named by the node, not inferred.** `n_in` and `n_out` mean
something different at every node - articles seen and kept, candidates in and
survivors out - so each adapter states its own two numbers rather than a wrapper
guessing them from the state. The screen labels them per node for the same
reason.

**A failed node still leaves a row.** The context manager writes on the way out
whether the body returned or raised, so a run that died at `rank` shows four
finished steps and a fifth carrying the exception. A run whose failure left no
trace of where it failed would be the one thing this table exists to prevent.

**A note is a key and its numbers, not a sentence.** The first cut wrote
`f"{n} restatement(s) dropped"` here, which put the one English string on a page
whose every other word comes from `i18n`. A node now records `note_key("dedupe",
dropped=n)` and the web layer writes the sentence in the reader's language. The
price is that a node with something new to say needs a string in two
dictionaries rather than an f-string, and that is the right price: the
alternative was an interface that switches to Turkish except for the column
nobody translated.

Raw machine output is the exception and stays raw - a feed's error text and an
exception's `repr` are not written in any language, and ADR 0011 already says
the monospaced face is where machine output lives. `note()` is that door.

Nothing here may raise into the pipeline. A bookkeeping insert that can kill a
paid run is worse than no bookkeeping - the same judgement ADR 0018 made about
tracing - so `_write` swallows and logs, and so does `record_fan_out`, which
reads two rows before it writes one and runs ahead of the node that persists the
digest.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select

from ainews.config import get_settings
from ainews.db import RunStep
from ainews.db.models import utcnow
from ainews.db.session import session_scope
from ainews.pipeline.pricing import estimate_cost
from ainews.pipeline.state import PipelineState

log = logging.getLogger(__name__)

MAX_DETAIL_CHARS = 300


@dataclass(slots=True)
class StepRecord:
    """The numbers a node reports about itself, filled in as it goes."""

    n_in: int | None = None
    n_out: int | None = None
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    detail: str | None = None
    status: str = "ok"
    # Set only by `step()` on the way out; a node never writes its own clock.
    started_at: datetime = field(default_factory=utcnow)

    def counts(self, n_in: int | None, n_out: int | None) -> None:
        self.n_in, self.n_out = n_in, n_out

    def spend(self, model: str, tokens_in: int, tokens_out: int) -> None:
        self.model, self.tokens_in, self.tokens_out = model, tokens_in, tokens_out

    def note(self, text: str) -> None:
        """Machine output, verbatim: a feed's error, an exception's repr.

        Anything a person wrote goes through `note_key` instead - see the module
        docstring. This door exists because a Python traceback is not a string
        that has a Turkish version.
        """
        self.detail = text[:MAX_DETAIL_CHARS]

    def note_key(self, key: str, **numbers: int) -> None:
        """A sentence the web layer will write, recorded as its parts.

        Stored as one compact JSON object in the same column, rather than as a
        second column: `create_all` adds tables and not columns (ADR 0005), and
        a `detail` that is sometimes structured and sometimes raw is a shape the
        reader already has to handle - `note()` writes into the same field.
        """
        self.detail = json.dumps({"k": key, **numbers}, separators=(",", ":"))[:MAX_DETAIL_CHARS]

    @property
    def est_cost_usd(self) -> float:
        if not self.model:
            return 0.0
        return estimate_cost(self.model, self.tokens_in, self.tokens_out)


async def _write(
    run_id: str, node: str, record: StepRecord, finished_at: datetime | None = None
) -> None:
    """One row, best effort. Never raises into the caller."""
    try:
        async with session_scope() as session:
            session.add(
                RunStep(
                    run_id=run_id,
                    node=node,
                    status=record.status,
                    started_at=record.started_at,
                    finished_at=finished_at or utcnow(),
                    n_in=record.n_in,
                    n_out=record.n_out,
                    model=record.model,
                    tokens_in=record.tokens_in,
                    tokens_out=record.tokens_out,
                    est_cost_usd=record.est_cost_usd,
                    detail=record.detail,
                )
            )
    except Exception:
        log.exception("could not record step %s of run %s", node, run_id)


@asynccontextmanager
async def step(run_id: str, node: str) -> AsyncIterator[StepRecord]:
    """Time one node and write its row, however the body ends.

    The body reports its own counts and spend on the yielded record; everything
    else - the two timestamps, the status of a node that raised - is filled in
    here.
    """
    record = StepRecord()
    try:
        yield record
    except Exception as exc:
        record.status = "error"
        record.note(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        await _write(run_id, node, record)


async def record_fan_out(state: PipelineState) -> None:
    """The summarize node's row, assembled after the fact. Never raises.

    Called before `persist_run` writes the digest, so the guard here is not
    decoration: an unhandled read error would take the whole paid run with it
    and leave nothing persisted, which is the opposite of what a bookkeeping
    table is for. `_write` swallows its own insert; this swallows the two
    lookups that precede it.

    The fan-out is the one node that cannot time itself: it is a hundred
    concurrent branches, and none of them knows when the first started or the
    last finished. Its span is therefore measured from the outside - from the
    moment `enrich` returned to the moment `rank` began, which is exactly the
    window LangGraph spends running the branches, plus the scheduling of one
    superstep either side.

    Called from `persist_node`, because by then both neighbours have rows and
    the state carries every branch's tokens.
    """
    try:
        await _fan_out_row(state)
    except Exception:
        log.exception("could not record the fan-out of run %s", state.get("run_id"))


async def _fan_out_row(state: PipelineState) -> None:
    run_id = state["run_id"]
    payloads = state.get("summaries") or []
    n_in = len(state.get("candidate_ids") or [])
    if not n_in:
        # The conditional edge jumped straight to `persist`: nothing was
        # summarised, so there is no step to describe.
        return

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(RunStep)
                .where(RunStep.run_id == run_id, RunStep.node.in_(("enrich", "rank")))
                .order_by(RunStep.started_at)
            )
        ).scalars()
        bounds = {row.node: row for row in rows}

    enrich, rank = bounds.get("enrich"), bounds.get("rank")
    if enrich is None or enrich.finished_at is None:
        log.warning("run %s: no bracket for the fan-out; step not recorded", run_id)
        return
    # `rank` returns early without a row when every branch failed, which is the
    # case where this step matters most. The fan-out ended a moment ago in that
    # case - `persist` is running - so close it on the clock rather than losing
    # the row that says a hundred branches produced nothing.
    ended = rank.started_at if rank is not None else utcnow()

    # One row per article even if a retried superstep appended a payload twice,
    # for the same reason `persist_run` collapses them: the count on screen has
    # to be the count of summaries that reached the database.
    n_out = len({p["article_id"] for p in payloads})
    record = StepRecord(started_at=enrich.finished_at)
    record.counts(n_in, n_out)
    # The same fallback `persist_run` uses, and it has to be the same one: that
    # node prices the run's total from `settings` when the state carries no
    # model, so an empty string here would cost the step at nothing while the
    # row above it charges for the tokens. `est_cost_usd` returns 0.0 for an
    # empty model, which is right for the four nodes that call nothing and wrong
    # for the one that just spent money.
    record.spend(
        state.get("model_summarize") or get_settings().openai_model_summarize,
        sum(p["tokens_in"] for p in payloads),
        sum(p["tokens_out"] for p in payloads),
    )
    if n_out < n_in:
        record.status = "partial"
        record.note_key("summarize_silent", n=n_in - n_out, of=n_in)
    await _write(run_id, "summarize", record, finished_at=ended)
