"""A summary belongs to an article; a bulletin belongs to a day.

The modelling fault ADR 0030 undoes. `summaries.run_id` tied a summary to the
press that made it and the front page showed a *run*, while candidate selection
is a delta over unsummarised articles - so the second press of a day produced a
two-story bulletin with its own brief, its own order and its own archive entry,
and three patches existed to keep the reader from seeing that.

This is the first revision that rebuilds a table rather than adding to one, and
the three things it has to get right are all consequences of that:

**The columns being added are NOT NULL onto a table with rows**, so each carries
a `server_default` for the copy and the default is then dropped: the application
supplies these on every insert and a default left in place is a second source
for a value.

**The bulletins have to be reconstructed before the columns that describe them
are dropped.** `rank` and `editor_importance` are the only record of what each
run published, and after the rebuild they are gone. Every run that produced
summaries becomes one bulletin, on the local day it started, at version 1.

**The FTS5 index does not survive a table rebuild.** Batch mode drops
`summaries` and recreates it, which takes its triggers with it and leaves the
contentless index pointing at rowids from a table that no longer exists. The
triggers are dropped first so the copy does not double-index, recreated after,
and the index is emptied and refilled from the rebuilt table.

**The archive's money stays on the bulletin.** A run recorded one total and the
per-summary split is not recoverable from it, so every reconstructed summary
costs 0.00 and the run's whole figure sits on the bulletin it published. New
summaries carry their own cost, so a day's spend is the bulletin plus its
items - a sum that is right going forward and understates nothing behind it,
because behind it the items are zero.

Revision ID: 0004_two_objects
Revises: 0003_provenance
Created: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from ainews.clock import local_day
from ainews.db.models import UTCDateTime

revision: str = "0004_two_objects"
down_revision: str | None = "0003_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FTS_TRIGGERS = ("summaries_fts_ai", "summaries_fts_ad", "summaries_fts_au")

FTS_TRIGGER_DDL = [
    """
    CREATE TRIGGER IF NOT EXISTS summaries_fts_ai AFTER INSERT ON summaries BEGIN
        INSERT INTO summaries_fts(rowid, title_local, summary, why_it_matters)
        VALUES (new.id, new.title_local, new.summary, new.why_it_matters);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS summaries_fts_ad AFTER DELETE ON summaries BEGIN
        INSERT INTO summaries_fts(summaries_fts, rowid, title_local, summary, why_it_matters)
        VALUES ('delete', old.id, old.title_local, old.summary, old.why_it_matters);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS summaries_fts_au AFTER UPDATE ON summaries BEGIN
        INSERT INTO summaries_fts(summaries_fts, rowid, title_local, summary, why_it_matters)
        VALUES ('delete', old.id, old.title_local, old.summary, old.why_it_matters);
        INSERT INTO summaries_fts(rowid, title_local, summary, why_it_matters)
        VALUES (new.id, new.title_local, new.summary, new.why_it_matters);
    END
    """,
]


def _refill_search_index() -> None:
    """Empty the contentless index and rebuild it from `summaries`.

    `'delete-all'` and not `DELETE FROM`: a contentless FTS5 table has no rows
    to delete, only terms to forget, and `'rebuild'` is unavailable because
    there is no content table to rebuild from.
    """
    op.execute("INSERT INTO summaries_fts(summaries_fts) VALUES('delete-all')")
    op.execute(
        "INSERT INTO summaries_fts(rowid, title_local, summary, why_it_matters)"
        " SELECT id, title_local, summary, why_it_matters FROM summaries"
    )


def _tier(position: int) -> str:
    """A place in a published order, read back as the tier it would have been.

    A reconstruction and not a measurement: these runs were ranked by a model
    that was asked for an order and a score, never for a tier. The bands are the
    shape the page already drew - one lead, a few majors, the rest - so the
    archive keeps the reading it had rather than acquiring a judgement nobody
    made.
    """
    if position == 1:
        return "lead"
    if position <= 4:
        return "major"
    if position <= 9:
        return "notable"
    return "brief"


def _local_day(stored: str | datetime) -> str:
    """The calendar day a run belongs to, as the reader counts days.

    A press at 01:30 in Antalya is 22:30 the previous day in the column, and it
    belongs to the day the reader is having. `ainews.clock` owns that rule; this
    only has to parse what SQLite handed back.
    """
    value = datetime.fromisoformat(stored) if isinstance(stored, str) else stored
    return local_day(value.replace(tzinfo=UTC) if value.tzinfo is None else value)


class DuplicateSummaries(RuntimeError):
    """Two summaries of one article in one language, which the new key forbids.

    Raised before anything is rebuilt rather than surfacing as an
    `IntegrityError` from the middle of a batch table copy, where the message
    names a constraint and not a row.

    The old key was `(article_id, run_id, language)`, so two runs could each
    hold a summary of one article and nothing in the schema said otherwise. What
    stopped it is `dedupe`, which selects only articles carrying no summary at
    all - a rule in one query, not a constraint. It is checked here because the
    cost of that rule ever having lapsed is a half-migrated database.

    **Rejected: collapsing them, keeping the newest.** `verdicts` and
    `eval_results` point at a summary, and `verdicts.summary_id` is unique - so
    re-pointing them would either fail or move a reader's own verdict onto text
    that reader never saw, which is a fabricated label in the one table the
    evaluation layer treats as ground truth.
    """


def _refuse_duplicates() -> None:
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT article_id, language, COUNT(*) FROM summaries"
                " GROUP BY article_id, language HAVING COUNT(*) > 1"
            )
        )
        .all()
    )
    if rows:
        listed = ", ".join(
            f"article {article} in {language} ({n})" for article, language, n in rows
        )
        raise DuplicateSummaries(
            f"{len(rows)} article/language pairs carry more than one summary: {listed}. "
            "uq_summary_article_lang cannot be created over them; resolve which "
            "summary is the article's before upgrading."
        )


def _rebuild_bulletins() -> None:
    connection = op.get_bind()
    runs = connection.execute(
        sa.text(
            "SELECT r.id, r.language, r.started_at, r.editor_note, r.model_rank, r.est_cost_usd"
            " FROM runs r"
            " WHERE EXISTS (SELECT 1 FROM summaries s WHERE s.run_id = r.id)"
            " ORDER BY r.started_at"
        )
    ).all()

    for run_id, language, started_at, editor_note, model_rank, cost in runs:
        day = _local_day(started_at)
        # `version` counts up within a day and language, so two presses on one
        # day in the old archive become two versions of that day rather than two
        # bulletins - which is the reading this revision exists to give.
        version = connection.execute(
            sa.text(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM bulletins"
                " WHERE day = :day AND language = :language"
            ),
            {"day": day, "language": language},
        ).scalar_one()
        bulletin_id = connection.execute(
            sa.text(
                "INSERT INTO bulletins (day, language, version, editor_note, model_rank,"
                " est_cost_usd, run_id, created_at)"
                " VALUES (:day, :language, :version, :note, :model, :cost, :run_id, :created)"
                " RETURNING id"
            ),
            {
                "day": day,
                "language": language,
                "version": version,
                "note": editor_note,
                "model": model_rank,
                "cost": cost or 0.0,
                "run_id": run_id,
                "created": started_at,
            },
        ).scalar_one()

        ranked = (
            connection.execute(
                sa.text(
                    "SELECT id FROM summaries"
                    " WHERE run_id = :run_id AND rank IS NOT NULL"
                    " ORDER BY rank"
                ),
                {"run_id": run_id},
            )
            .scalars()
            .all()
        )
        for position, summary_id in enumerate(ranked, start=1):
            connection.execute(
                sa.text(
                    "INSERT INTO bulletin_items (bulletin_id, summary_id, position, tier)"
                    " VALUES (:bulletin, :summary, :position, :tier)"
                ),
                {
                    "bulletin": bulletin_id,
                    "summary": summary_id,
                    "position": position,
                    "tier": _tier(position),
                },
            )


def upgrade() -> None:
    _refuse_duplicates()

    op.create_table(
        "bulletins",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column("language", sa.String(length=2), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("editor_note", sa.Text(), nullable=True),
        sa.Column("model_rank", sa.String(length=60), nullable=True),
        sa.Column("agreement", sa.Float(), nullable=True),
        sa.Column("est_cost_usd", sa.Float(), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("day", "language", "version", name="uq_bulletin_day_lang_version"),
    )
    with op.batch_alter_table("bulletins", schema=None) as batch_op:
        batch_op.create_index("ix_bulletins_day", ["day", "language", "version"], unique=False)

    op.create_table(
        "bulletin_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bulletin_id", sa.Integer(), nullable=False),
        sa.Column("summary_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("tier", sa.String(length=10), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "tier in ('lead', 'major', 'notable', 'brief')", name="ck_bulletin_items_tier"
        ),
        sa.ForeignKeyConstraint(["bulletin_id"], ["bulletins.id"]),
        sa.ForeignKeyConstraint(["summary_id"], ["summaries.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bulletin_id", "summary_id", name="uq_item_bulletin_summary"),
    )
    with op.batch_alter_table("bulletin_items", schema=None) as batch_op:
        batch_op.create_index(
            "ix_bulletin_items_bulletin", ["bulletin_id", "position"], unique=False
        )

    # Before the columns that describe them are dropped.
    _rebuild_bulletins()

    for trigger in FTS_TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")

    with op.batch_alter_table("summaries", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("relevant", sa.Boolean(), nullable=False, server_default=sa.text("1"))
        )
        batch_op.add_column(
            sa.Column("kind", sa.String(length=12), nullable=False, server_default="news")
        )
        batch_op.add_column(
            sa.Column("model", sa.String(length=60), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("tokens_in", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(
            sa.Column("tokens_out", sa.Integer(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.add_column(
            sa.Column("est_cost_usd", sa.Float(), nullable=False, server_default=sa.text("0"))
        )
        batch_op.create_check_constraint(
            "ck_summaries_kind",
            "kind in ('news', 'release', 'research', 'opinion', 'roundup', 'other')",
        )
        batch_op.drop_index("ix_summaries_run_rank")
        batch_op.drop_constraint("uq_summary_article_run_lang", type_="unique")
        batch_op.create_index("ix_summaries_article", ["article_id"], unique=False)
        batch_op.create_unique_constraint("uq_summary_article_lang", ["article_id", "language"])
        batch_op.drop_column("run_id")
        batch_op.drop_column("rank")
        batch_op.drop_column("editor_importance")

    # The defaults were for the copy. The application writes every one of these
    # on insert, and a default left behind is a second place a value comes from.
    with op.batch_alter_table("summaries", schema=None) as batch_op:
        for column in ("relevant", "kind", "model", "tokens_in", "tokens_out", "est_cost_usd"):
            batch_op.alter_column(column, server_default=None)

    for statement in FTS_TRIGGER_DDL:
        op.execute(statement)
    _refill_search_index()

    # A measurement is about what was published, not about the press that paid
    # for it. Backfilled from the bulletin each run wrote above, which is the
    # only moment that mapping exists in one place.
    with op.batch_alter_table("eval_results", schema=None) as batch_op:
        batch_op.add_column(sa.Column("bulletin_id", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE eval_results SET bulletin_id ="
        " (SELECT b.id FROM bulletins b WHERE b.run_id = eval_results.run_id"
        "  ORDER BY b.version DESC LIMIT 1)"
    )
    with op.batch_alter_table("eval_results", schema=None) as batch_op:
        batch_op.create_foreign_key(
            "fk_eval_results_bulletin", "bulletins", ["bulletin_id"], ["id"]
        )
        batch_op.drop_index("ix_eval_results_run_kind")
        batch_op.create_index(
            "ix_eval_results_bulletin_kind", ["bulletin_id", "kind"], unique=False
        )
        batch_op.drop_column("run_id")


def downgrade() -> None:
    with op.batch_alter_table("eval_results", schema=None) as batch_op:
        # Nullable on the way back: a measurement taken after this revision
        # belongs to a bulletin that may have been republished since, and the
        # run it names would be a guess.
        batch_op.add_column(sa.Column("run_id", sa.VARCHAR(length=32), nullable=True))
        batch_op.drop_index("ix_eval_results_bulletin_kind")
        batch_op.create_index("ix_eval_results_run_kind", ["run_id", "kind"], unique=False)
        batch_op.drop_constraint("fk_eval_results_bulletin", type_="foreignkey")
    op.execute(
        "UPDATE eval_results SET run_id ="
        " (SELECT b.run_id FROM bulletins b WHERE b.id = eval_results.bulletin_id)"
    )
    with op.batch_alter_table("eval_results", schema=None) as batch_op:
        batch_op.drop_column("bulletin_id")

    for trigger in FTS_TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")

    with op.batch_alter_table("summaries", schema=None) as batch_op:
        batch_op.add_column(sa.Column("editor_importance", sa.INTEGER(), nullable=True))
        batch_op.add_column(sa.Column("rank", sa.INTEGER(), nullable=True))
        # Nullable on the way back, unlike the column that was dropped: the run
        # a summary was written by is not recoverable for anything published
        # after this revision, and a NOT NULL column would have to invent one.
        batch_op.add_column(sa.Column("run_id", sa.VARCHAR(length=32), nullable=True))
        batch_op.drop_constraint("uq_summary_article_lang", type_="unique")
        batch_op.drop_index("ix_summaries_article")
        batch_op.drop_constraint("ck_summaries_kind", type_="check")
        batch_op.create_unique_constraint(
            "uq_summary_article_run_lang", ["article_id", "run_id", "language"]
        )
        batch_op.create_index("ix_summaries_run_rank", ["run_id", "rank"], unique=False)
        batch_op.drop_column("est_cost_usd")
        batch_op.drop_column("tokens_out")
        batch_op.drop_column("tokens_in")
        batch_op.drop_column("model")
        batch_op.drop_column("kind")
        batch_op.drop_column("relevant")

    # What the bulletins knew, written back onto the summaries.
    op.execute(
        "UPDATE summaries SET"
        " run_id = (SELECT b.run_id FROM bulletin_items i JOIN bulletins b"
        " ON b.id = i.bulletin_id WHERE i.summary_id = summaries.id),"
        " rank = (SELECT i.position FROM bulletin_items i WHERE i.summary_id = summaries.id)"
    )

    for statement in FTS_TRIGGER_DDL:
        op.execute(statement)
    _refill_search_index()

    with op.batch_alter_table("bulletin_items", schema=None) as batch_op:
        batch_op.drop_index("ix_bulletin_items_bulletin")
    op.drop_table("bulletin_items")
    with op.batch_alter_table("bulletins", schema=None) as batch_op:
        batch_op.drop_index("ix_bulletins_day")
    op.drop_table("bulletins")
