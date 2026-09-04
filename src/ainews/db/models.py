"""SQLAlchemy 2 models.

Four tables carry the pipeline: `sources` (what to poll), `articles` (what came
back), `summaries` (what the LLM made of it) and `runs` (what each execution did
and cost). A fifth, `daily_counters`, is the durable side of the Tavily credit
cap - it has to survive a restart, so it cannot live in memory.

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
    `thread_id`, so a crashed run can be resumed by name.
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


class Summary(Base):
    """The LLM's read of one article, in one language, for one run.

    `rank` is set only for the items that made the digest's top N; everything
    else keeps its `importance` and is reachable below the fold and in search.
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
    rank: Mapped[int | None] = mapped_column(Integer, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    article: Mapped[Article] = relationship(back_populates="summaries")
    run: Mapped[Run] = relationship(back_populates="summaries")

    __table_args__ = (
        UniqueConstraint("article_id", "run_id", "language", name="uq_summary_article_run_lang"),
        CheckConstraint("importance between 1 and 5", name="ck_summaries_importance"),
        Index("ix_summaries_run_rank", "run_id", "rank"),
    )


class DailyCounter(Base):
    """Per-day spend counters that must survive a restart (Tavily's credit cap)."""

    __tablename__ = "daily_counters"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # UTC YYYY-MM-DD
    tavily_credits: Mapped[int] = mapped_column(Integer, default=0)
