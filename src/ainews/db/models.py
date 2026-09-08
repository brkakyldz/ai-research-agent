"""SQLAlchemy 2 models.

Four tables carry the pipeline: `sources` (what to poll), `articles` (what came
back), `summaries` (what the LLM made of it) and `runs` (what each execution did
and cost). `run_steps` breaks a run down by graph node, which is what the run
detail page reads (ADR 0022). A sixth, `daily_counters`, is the durable side of
the Tavily credit cap - it has to survive a restart, so it cannot live in memory. Two more belong
to the evaluation layer: `verdicts`, the reader's own call on a summary, and
`eval_results`, what the sampled judge and the rank probe measured and what it
cost (PLAN-EVALS E2, E3).

An FTS5 virtual table mirrors `summaries` for the dashboard's search box; it is
created and kept in sync by triggers in `ainews.db.schema`, not by the ORM.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

SourceKind = Literal["rss", "tavily"]
RunKind = Literal["collect", "digest", "manual"]
RunStatus = Literal["running", "ok", "partial", "error"]


def utcnow() -> datetime:
    """Timezone-aware UTC now. Every stored timestamp is UTC; only the UI localises."""
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Source(Base):
    """A feed to poll.

    `etag` / `modified` are the conditional-GET tokens from the last successful
    fetch; sending them back is what turns a three-hourly poll into a 304 rather
    than a full download, which is what keeps publishers from banning us.
    """

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(1000), unique=True)
    kind: Mapped[str] = mapped_column(String(20), default="rss")
    # Editorial weight, 0.5-2.0: breaks ranking ties and picks enrichment targets.
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    enabled: Mapped[bool] = mapped_column(default=True)

    etag: Mapped[str | None] = mapped_column(String(500), default=None)
    modified: Mapped[str | None] = mapped_column(String(200), default=None)
    last_status: Mapped[str | None] = mapped_column(String(200), default=None)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    articles: Mapped[list[Article]] = relationship(back_populates="source")

    __table_args__ = (
        CheckConstraint("kind in ('rss', 'tavily')", name="ck_sources_kind"),
        CheckConstraint("weight > 0", name="ck_sources_weight"),
    )


class Article(Base):
    """One item from one feed.

    `url_canonical` is the deduplication key at the database level: tracking
    parameters stripped, host lowercased. `dup_of` records the *fuzzy* duplicate
    decision instead of deleting the row, so a wrong merge stays inspectable.
    """

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    url_canonical: Mapped[str] = mapped_column(String(1000), unique=True)
    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    body_text: Mapped[str | None] = mapped_column(Text, default=None)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Set when this article was judged a restatement of an earlier one.
    dup_of: Mapped[int | None] = mapped_column(ForeignKey("articles.id"), default=None)

    source: Mapped[Source] = relationship(back_populates="articles")
    summaries: Mapped[list[Summary]] = relationship(back_populates="article")

    __table_args__ = (
        Index("ix_articles_fetched_at", "fetched_at"),
        Index("ix_articles_published_at", "published_at"),
        Index("ix_articles_dup_of", "dup_of"),
    )


class Run(Base):
    """One execution of the pipeline.

    The id is a uuid hex string and doubles as the LangGraph checkpointer
    `thread_id`, so a run that failed can be resumed by name - `ainews digest
    --resume <id>`, or the same offer inside the confirmation on `/runs`
    (`pipeline/runner.py`, `run_digest(resume=...)`).
    """

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(20))
    language: Mapped[str] = mapped_column(String(2), default="tr")
    status: Mapped[str] = mapped_column(String(20), default="running")

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    n_collected: Mapped[int] = mapped_column(Integer, default=0)
    n_new: Mapped[int] = mapped_column(Integer, default=0)
    n_summarized: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    est_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    editor_note: Mapped[str | None] = mapped_column(Text, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    summaries: Mapped[list[Summary]] = relationship(back_populates="run")

    __table_args__ = (
        CheckConstraint("kind in ('collect', 'digest', 'manual')", name="ck_runs_kind"),
        Index("ix_runs_started_at", "started_at"),
    )

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


class RunStep(Base):
    """One node of the digest graph, as it actually ran.

    The `runs` row says what a run cost in total; this says where the time and
    the money went inside it. There is one row per node and not one per model
    call: the summarize node runs a hundred branches wide, and a hundred inserts
    from a hundred concurrent branches against SQLite's single writer (ADR 0003)
    would be contention bought for a level of detail nothing on screen shows.
    Per-call detail is Phoenix's job (ADR 0018).

    Rows are ordered by `started_at` rather than by a sequence number. The graph
    is linear, so the clock already puts the nodes in their order - and the
    fan-out's row, whose span is measured between its neighbours rather than
    inside itself, sorts into the right place for free.

    `n_in` and `n_out` are whatever the node counts, which differs by node and is
    named on screen: articles seen and kept for `collect`, candidates in and
    survivors out for `dedupe`. `model` is empty and cost zero for the four nodes
    that call nothing.
    """

    __tablename__ = "run_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    node: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="ok")

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    n_in: Mapped[int | None] = mapped_column(Integer, default=None)
    n_out: Mapped[int | None] = mapped_column(Integer, default=None)

    model: Mapped[str] = mapped_column(String(60), default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    est_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # The error that stopped the node, or a one-line note from a node that has
    # something to say beyond two counts - how many bodies Tavily was asked for,
    # how many branches came back empty.
    detail: Mapped[str | None] = mapped_column(Text, default=None)

    run: Mapped[Run] = relationship()

    __table_args__ = (
        CheckConstraint("status in ('ok', 'partial', 'error')", name="ck_run_steps_status"),
        Index("ix_run_steps_run", "run_id", "started_at"),
    )

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


class Summary(Base):
    """The LLM's read of one article, in one language, for one run.

    `rank` is set only for the items that made the digest's top N; everything
    else keeps its `importance` and is reachable below the fold and in search.

    Two scores (ADR 0025). `importance` is the summariser's, given with one
    article in view and nothing else. `editor_importance` is the ranker's read
    of the same story against the whole day, set only on ranked items; the page
    draws it where it exists and the summariser's score where it does not. The
    first is kept because the evaluation layer measures the summariser and the
    ranker separately - the free importance-then-weight order the rank call is
    compared against has to be built from the score the rank call did not
    write. Nullable and unconstrained at the database because it was added to
    a live archive by `ALTER TABLE` (`db/schema.py`); the schema on the model
    holds it to 1-5 before it gets here.
    """

    __tablename__ = "summaries"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    language: Mapped[str] = mapped_column(String(2))

    title_local: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    why_it_matters: Mapped[str] = mapped_column(Text)
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    importance: Mapped[int] = mapped_column(Integer, default=3)
    editor_importance: Mapped[int | None] = mapped_column(Integer, default=None)
    rank: Mapped[int | None] = mapped_column(Integer, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    article: Mapped[Article] = relationship(back_populates="summaries")
    run: Mapped[Run] = relationship(back_populates="summaries")

    @property
    def shown_importance(self) -> int:
        """The score the page draws: the editor's where there is one."""
        return self.editor_importance if self.editor_importance is not None else self.importance

    __table_args__ = (
        UniqueConstraint("article_id", "run_id", "language", name="uq_summary_article_run_lang"),
        CheckConstraint("importance between 1 and 5", name="ck_summaries_importance"),
        Index("ix_summaries_run_rank", "run_id", "rank"),
    )


class Verdict(Base):
    """The reader's own call on one summary: right, or wrong.

    Binary, not a 1-5 score - a person reading a digest can say "that is
    wrong" in one click and cannot honestly say "that is a 3", and the
    grounding judge (`ainews eval judge --labelled`) is calibrated against
    exactly this yes/no. One row per summary: a later verdict overwrites the
    earlier one rather than accumulating a history nobody reads. `note` is the
    reader's reason, free text, and it is the raw material a judge prompt is
    rewritten from when its numbers say it should be (PLAN-EVALS E2, E5).
    """

    __tablename__ = "verdicts"

    id: Mapped[int] = mapped_column(primary_key=True)
    summary_id: Mapped[int] = mapped_column(ForeignKey("summaries.id"), unique=True)
    verdict: Mapped[str] = mapped_column(String(10))
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    summary: Mapped[Summary] = relationship()

    __table_args__ = (CheckConstraint("verdict in ('ok', 'wrong')", name="ck_verdicts_verdict"),)


class EvalResult(Base):
    """One measurement that cost money: a judged summary, or a rank probe.

    Eval spend is on the record the same way product spend is, so
    `ainews eval report` can total the two side by side. `summary_id` is null
    for a `rank_stability` row, which is about a run rather than a summary.
    `passed` is null when the judge answered but could not be parsed - recorded,
    not raised, so a bad afternoon at the API is a row and not a traceback.
    """

    __tablename__ = "eval_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    summary_id: Mapped[int | None] = mapped_column(ForeignKey("summaries.id"), default=None)
    kind: Mapped[str] = mapped_column(String(20))
    passed: Mapped[bool | None] = mapped_column(default=None)
    # The unsupported claim for a grounding row; tau and overlap as JSON for a
    # rank-stability row.
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    model: Mapped[str] = mapped_column(String(60), default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    est_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("kind in ('grounding', 'rank_stability')", name="ck_eval_results_kind"),
        Index("ix_eval_results_run_kind", "run_id", "kind"),
    )


class DailyCounter(Base):
    """Per-day spend counters that must survive a restart (Tavily's credit cap)."""

    __tablename__ = "daily_counters"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # UTC YYYY-MM-DD
    tavily_credits: Mapped[int] = mapped_column(Integer, default=0)
