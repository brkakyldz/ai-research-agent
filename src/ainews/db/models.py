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
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Select,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

# One value, and it is not an oversight. `tavily` was in this literal and in the
# CHECK from M0 and nothing ever wrote it: Tavily is an *enrichment* step run over
# an article whose body a feed served thin (`sources/tavily.py`), not a thing that
# is polled on a schedule and appears in this table. The column stays because
# removing it is a table rebuild, which is the operation ADR 0005 says needs
# Alembic first; what it stops doing is naming a kind of row that has never
# existed.
SourceKind = Literal["rss"]
# Two kinds, because there are two kinds of work: the free feed poll and the run
# that spends money. There was a third, `manual`, distinguishing a pressed digest
# from a scheduled one - vestigial since ADR 0015 took the clock off and nothing
# schedules a digest any more. It was worse than merely spare: the button and the
# CLI both wrote `manual` while the rail's archive badge counted `digest`, so the
# badge said 1 above an archive listing 3.
RunKind = Literal["collect", "digest"]
RunStatus = Literal["running", "ok", "partial", "error"]


def utcnow() -> datetime:
    """Timezone-aware UTC now. Every stored timestamp is UTC; only the UI localises."""
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """A timestamp that comes back the way it went in: aware, in UTC.

    `DateTime(timezone=True)` is a promise the backend has to keep, and SQLite
    has no timestamp type to keep it with - it stores a string, and every value
    read back is naive. The `timezone=True` on ten columns was therefore a no-op,
    and the consequence was a guard at each read: `to_local`,
    `latest_digest_run` and `build_advice` each re-attached UTC by hand, the
    three of them agreeing by luck rather than by rule.

    Where it had already gone wrong is `steps._fan_out_row`, which brackets the
    summarise fan-out between two other nodes' timestamps: `rank.started_at`
    came back naive and the fallback was `utcnow()`, aware. The subtraction that
    follows raises `TypeError` on exactly the path that matters most - the one
    taken when every branch of the fan-out failed and there is no `rank` row.

    So the conversion belongs at the boundary, once, in both directions. A naive
    value going in is read as UTC, because that is what `utcnow()` produces and
    what every writer in this codebase means; an aware one is converted rather
    than truncated. Coming out, UTC is re-attached. Nothing above this line has
    to think about it again.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


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
    # Editorial weight, 0.5-2.0: breaks ranking ties, decides which thin articles
    # are worth a Tavily credit, and since ADR 0025 picks the survivor of a
    # duplicate cluster. Editable on `/sources`; it was documented as a range,
    # constrained only above zero, and settable nowhere.
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    enabled: Mapped[bool] = mapped_column(default=True)

    etag: Mapped[str | None] = mapped_column(String(500), default=None)
    modified: Mapped[str | None] = mapped_column(String(200), default=None)
    last_status: Mapped[str | None] = mapped_column(String(200), default=None)
    last_fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    articles: Mapped[list[Article]] = relationship(back_populates="source")

    __table_args__ = (
        CheckConstraint("kind in ('rss')", name="ck_sources_kind"),
        # The range the editor offers and the docstring names, enforced. It was
        # `weight > 0` alone, so a typo in a form field could set 20.0 and make
        # one feed outrank every other by an order of magnitude in a tie-break
        # nobody would think to look at.
        CheckConstraint("weight >= 0.5 AND weight <= 2.0", name="ck_sources_weight"),
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
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    # The article, and only the article. Tavily's snippets used to be appended
    # here with no marker, which made `body_text` "the article plus whatever a
    # search for its title returned this week" - and the grounding judge reads
    # this field as "the text the summariser was shown". A claim that came from
    # a bad snippet was therefore judged grounded (ADR 0029).
    body_text: Mapped[str | None] = mapped_column(Text, default=None)
    # Third-party text found by searching for the title. The summariser is shown
    # it, labelled; the judge and the fixture recorder never are.
    extra_text: Mapped[str | None] = mapped_column(Text, default=None)
    # Where `body_text` came from: `feed`, `fetch`, or `unknown`. `unknown` is
    # not a failure state, it is the truthful value for every row written before
    # the split - their bodies may or may not carry search text and there is no
    # marker to tell, so nothing downstream may claim they are article text.
    body_source: Mapped[str | None] = mapped_column(String(10), default=None)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
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

    A run is what was *done*, not what was published: `bulletins` is the
    published object (ADR 0030). What stays here is the machine's own account of
    the press - what it collected, what it summarised, what it cost, whether it
    finished - which is what `/runs` reads and what `run_steps` hangs off.
    """

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(20))
    language: Mapped[str] = mapped_column(String(2), default="tr")
    status: Mapped[str] = mapped_column(String(20), default="running")

    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)

    n_collected: Mapped[int] = mapped_column(Integer, default=0)
    n_new: Mapped[int] = mapped_column(Integer, default=0)
    n_summarized: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    est_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # Which models this run was told to use (ADR 0020). Written at the press,
    # before any node runs, so the row says what was bought even when the run
    # failed at `collect` and left no `run_steps` to infer it from - which is
    # what `evals/record.py` had to do, and what made a fixture's model field a
    # reconstruction rather than a record. Nullable: every run before 2026-09-08
    # has neither, and those are still read off the steps.
    model_summarize: Mapped[str | None] = mapped_column(String(60), default=None)
    model_rank: Mapped[str | None] = mapped_column(String(60), default=None)

    editor_note: Mapped[str | None] = mapped_column(Text, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    # No `summaries` here any more. A summary outlives the run that wrote it
    # (ADR 0030), and a run's own output is the bulletin it published.
    bulletins: Mapped[list[Bulletin]] = relationship()

    __table_args__ = (
        CheckConstraint("kind in ('collect', 'digest')", name="ck_runs_kind"),
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

    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)

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
    """The LLM's read of one article, in one language. Written once.

    It used to be keyed on the run that made it, and the front page showed a
    run. That is the modelling fault ADR 0030 undoes: candidate selection is a
    delta over unsummarised articles, so the second press of a day produced a
    two-story *bulletin* - its own brief, its own order, its own archive entry -
    and three separate patches existed to hide that from the reader. A summary
    is a property of an article; which stories make a day's bulletin, in what
    order, is a different object.

    So `run_id`, `rank` and `editor_importance` are gone from here.
    `bulletin_items` holds the last two, because they are facts about a
    bulletin rather than about a summary, and a day can be re-ranked without
    rewriting the text.

    `importance` stays and is the summariser's own score, given with one
    article in view and nothing else. `relevant` and `kind` are what let the
    ranker select rather than fill: the source list was the only thing deciding
    whether an item was AI news, and one weight-1.5 source is a personal blog
    that also carries map projections.
    """

    __tablename__ = "summaries"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    language: Mapped[str] = mapped_column(String(2))

    title_local: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    why_it_matters: Mapped[str] = mapped_column(Text)
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    importance: Mapped[int] = mapped_column(Integer, default=3)
    # Is this AI news at all, and what shape of item is it. The ranker is given
    # only the relevant ones, and a roundup is never offered the lead.
    relevant: Mapped[bool] = mapped_column(Boolean, default=True)
    kind: Mapped[str] = mapped_column(String(12), default="news")

    # What this one summary cost. It used to be totalled onto the run and
    # nowhere else, which was fine while a summary belonged to a run; now that
    # it outlives one, the cost has to travel with it or a re-ranked day would
    # look free.
    model: Mapped[str] = mapped_column(String(60), default="")
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    est_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    article: Mapped[Article] = relationship(back_populates="summaries")

    __table_args__ = (
        UniqueConstraint("article_id", "language", name="uq_summary_article_lang"),
        CheckConstraint("importance between 1 and 5", name="ck_summaries_importance"),
        CheckConstraint(
            "kind in ('news', 'release', 'research', 'opinion', 'roundup', 'other')",
            name="ck_summaries_kind",
        ),
        Index("ix_summaries_article", "article_id"),
    )


class Bulletin(Base):
    """One day's reading, in one language: a ranking over the summaries in view.

    Replaceable, which is the whole point. A press summarises whatever is new
    and re-ranks the day, writing a new `version` rather than a second bulletin
    - so pressing twice before lunch leaves one page, not a bulletin and a
    two-story supplement the front page then has to choose between.

    `agreement` is how much the three shuffled rank calls agreed with the order
    that shipped. It is a production number rather than an occasional probe, and
    it is also what marks a run whose rank call failed: the fallback order ships
    with no editor behind it, and the page draws editor-sized headlines either
    way.
    """

    __tablename__ = "bulletins"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The local calendar day, `YYYY-MM-DD`. A string and not a date, because
    # every clock the interface draws is local (ADR 0016) and a date column
    # would invite a UTC comparison against it.
    day: Mapped[str] = mapped_column(String(10))
    language: Mapped[str] = mapped_column(String(2))
    version: Mapped[int] = mapped_column(Integer, default=1)

    editor_note: Mapped[str | None] = mapped_column(Text, default=None)
    model_rank: Mapped[str | None] = mapped_column(String(60), default=None)
    agreement: Mapped[float | None] = mapped_column(Float, default=None)
    est_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    items: Mapped[list[BulletinItem]] = relationship(
        back_populates="bulletin", cascade="all, delete-orphan", order_by="BulletinItem.position"
    )

    __table_args__ = (
        UniqueConstraint("day", "language", "version", name="uq_bulletin_day_lang_version"),
        Index("ix_bulletins_day", "day", "language", "version"),
    )


class BulletinItem(Base):
    """One story's place in one bulletin.

    `tier` and not a second importance score (ADR 0030). The summariser is told
    to be honest and that most items are 2 or 3; the ranker is told that a 4
    leading the day is a 5. The page used to sort and size by
    `coalesce(editor_importance, importance)`, which mixed an absolute scale
    with a relative one - a ranked 3-that-became-5 drawn larger than an honest
    unranked 3, in a layout whose only ranking indicator is size. A bulletin
    item draws its tier; anything outside the bulletin draws its significance;
    nothing coalesces.

    `reason` is the ranker's own sentence for the pick. It is stored because
    "prefer one strong story over three angles" was a rule with no trace: the
    ranker's cluster decisions left nothing behind to check.
    """

    __tablename__ = "bulletin_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    bulletin_id: Mapped[int] = mapped_column(ForeignKey("bulletins.id"))
    summary_id: Mapped[int] = mapped_column(ForeignKey("summaries.id"))
    position: Mapped[int] = mapped_column(Integer)
    tier: Mapped[str] = mapped_column(String(10), default="notable")
    reason: Mapped[str | None] = mapped_column(Text, default=None)

    bulletin: Mapped[Bulletin] = relationship(back_populates="items")
    summary: Mapped[Summary] = relationship()

    __table_args__ = (
        UniqueConstraint("bulletin_id", "summary_id", name="uq_item_bulletin_summary"),
        CheckConstraint(
            "tier in ('lead', 'major', 'notable', 'brief')", name="ck_bulletin_items_tier"
        ),
        Index("ix_bulletin_items_bulletin", "bulletin_id", "position"),
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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    summary: Mapped[Summary] = relationship()

    __table_args__ = (CheckConstraint("verdict in ('ok', 'wrong')", name="ck_verdicts_verdict"),)


class EvalResult(Base):
    """One measurement that cost money: a judged summary, or a rank probe.

    Eval spend is on the record the same way product spend is, so
    `ainews eval report` can total the two side by side. `summary_id` is null
    for a `rank_stability` row, which is about a whole bulletin rather than one
    summary. `passed` is null when the judge answered but could not be parsed -
    recorded, not raised, so a bad afternoon at the API is a row and not a
    traceback.

    Keyed on the bulletin and not on the run that made it (ADR 0030). What is
    measured is what was published: a grounding pass is a claim about a story on
    a page, and a stability number is a claim about the order that page is in.
    Nullable because a bulletin can be deleted from a demo database while its
    measurements are what the demo is showing.
    """

    __tablename__ = "eval_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    bulletin_id: Mapped[int | None] = mapped_column(ForeignKey("bulletins.id"), default=None)
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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint("kind in ('grounding', 'rank_stability')", name="ck_eval_results_kind"),
        Index("ix_eval_results_bulletin_kind", "bulletin_id", "kind"),
    )


class DailyCounter(Base):
    """Per-day spend counters that must survive a restart (Tavily's credit cap)."""

    __tablename__ = "daily_counters"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # UTC YYYY-MM-DD
    tavily_credits: Mapped[int] = mapped_column(Integer, default=0)


def bulletin_runs() -> Select[tuple[Run]]:
    """Every run that is a bulletin rather than a feed poll, newest first.

    The one place the distinction is written down. Five callers filtered `runs`
    their own way and two of them were wrong - the rail badge counted a `kind`
    nothing wrote, and `/runs/status` read `manual` only, so a run started at a
    terminal never reported its error there.

    It lives beside the model rather than in `web/queries.py` because the
    pipeline asks the same question - which run is the latest - and the pipeline
    must not import the web layer to ask it.

    Callers add their own conditions: `.where(Run.status != "running")` for a
    run that has finished, `.where(Run.n_summarized > 0)` for one that produced
    something. What none of them repeat is the `kind` test.
    """
    return select(Run).where(Run.kind == "digest").order_by(Run.started_at.desc())
