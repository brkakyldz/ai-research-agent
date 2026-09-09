"""The schema as it stood when migrations began.

Every table here already existed on 2026-09-09, created by `create_all` and
amended twice by hand (ADR 0005 and its two narrow amendments: `ADDED_COLUMNS`
for `summaries.editor_importance` and the two model columns on `runs`,
`DATA_FIXUPS` for the retired `manual` run kind). So this revision is written to
be *stampable*: an existing archive is marked as being at this point rather than
having it applied, and a fresh database gets it applied in full. The two have to
agree, and `tests/test_migrations.py` is what checks that they do.

One difference is accepted rather than fixed. SQLite bakes a CHECK constraint
into the table's DDL, so an archive created before ADR 0026 still carries
`kind in ('collect', 'digest', 'manual')` while a fresh database gets the
two-value form written below. The looser constraint admits everything the code
now writes, and rewriting a table to tighten a constraint is exactly the
operation this migration exists to make possible later - not one to spend on
history.

The FTS5 index is here rather than in application code because it is part of the
schema at this revision. It is a contentless virtual table plus three triggers:
the text lives once, in `summaries`, and `rowid` is the summary id so a hit
joins straight back.

Revision ID: 0001_baseline
Revises:
Created: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from ainews.db.models import UTCDateTime

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Contentless FTS5 over `summaries`, and the three triggers that keep it in
# step. Written out here rather than imported from application code: a
# migration is a snapshot, and a later revision that changes which columns are
# indexed must not silently rewrite what this one did.
FTS_DDL = [
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts USING fts5(
        title_local,
        summary,
        why_it_matters,
        content='',
        tokenize='unicode61 remove_diacritics 2'
    )
    """,
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

FTS_DROP = [
    "DROP TRIGGER IF EXISTS summaries_fts_au",
    "DROP TRIGGER IF EXISTS summaries_fts_ad",
    "DROP TRIGGER IF EXISTS summaries_fts_ai",
    "DROP TABLE IF EXISTS summaries_fts",
]


def upgrade() -> None:
    op.create_table(
        "daily_counters",
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column("tavily_credits", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("day"),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("language", sa.String(length=2), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", UTCDateTime(), nullable=False),
        sa.Column("finished_at", UTCDateTime(), nullable=True),
        sa.Column("n_collected", sa.Integer(), nullable=False),
        sa.Column("n_new", sa.Integer(), nullable=False),
        sa.Column("n_summarized", sa.Integer(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("est_cost_usd", sa.Float(), nullable=False),
        sa.Column("model_summarize", sa.String(length=60), nullable=True),
        sa.Column("model_rank", sa.String(length=60), nullable=True),
        sa.Column("editor_note", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("kind in ('collect', 'digest')", name="ck_runs_kind"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.create_index("ix_runs_started_at", ["started_at"], unique=False)

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("etag", sa.String(length=500), nullable=True),
        sa.Column("modified", sa.String(length=200), nullable=True),
        sa.Column("last_status", sa.String(length=200), nullable=True),
        sa.Column("last_fetched_at", UTCDateTime(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.CheckConstraint("kind in ('rss')", name="ck_sources_kind"),
        sa.CheckConstraint("weight >= 0.5 AND weight <= 2.0", name="ck_sources_weight"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url"),
    )
    op.create_table(
        "articles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("url_canonical", sa.String(length=1000), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("published_at", UTCDateTime(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("fetched_at", UTCDateTime(), nullable=False),
        sa.Column("dup_of", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["dup_of"],
            ["articles.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url_canonical"),
    )
    with op.batch_alter_table("articles", schema=None) as batch_op:
        batch_op.create_index("ix_articles_dup_of", ["dup_of"], unique=False)
        batch_op.create_index("ix_articles_fetched_at", ["fetched_at"], unique=False)
        batch_op.create_index("ix_articles_published_at", ["published_at"], unique=False)

    op.create_table(
        "run_steps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("node", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", UTCDateTime(), nullable=False),
        sa.Column("finished_at", UTCDateTime(), nullable=True),
        sa.Column("n_in", sa.Integer(), nullable=True),
        sa.Column("n_out", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(length=60), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("est_cost_usd", sa.Float(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.CheckConstraint("status in ('ok', 'partial', 'error')", name="ck_run_steps_status"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("run_steps", schema=None) as batch_op:
        batch_op.create_index("ix_run_steps_run", ["run_id", "started_at"], unique=False)

    op.create_table(
        "summaries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("article_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("language", sa.String(length=2), nullable=False),
        sa.Column("title_local", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("why_it_matters", sa.Text(), nullable=False),
        sa.Column("tags_json", sa.Text(), nullable=False),
        sa.Column("importance", sa.Integer(), nullable=False),
        sa.Column("editor_importance", sa.Integer(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("importance between 1 and 5", name="ck_summaries_importance"),
        sa.ForeignKeyConstraint(
            ["article_id"],
            ["articles.id"],
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("article_id", "run_id", "language", name="uq_summary_article_run_lang"),
    )
    with op.batch_alter_table("summaries", schema=None) as batch_op:
        batch_op.create_index("ix_summaries_run_rank", ["run_id", "rank"], unique=False)

    op.create_table(
        "eval_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("summary_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=60), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("est_cost_usd", sa.Float(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("kind in ('grounding', 'rank_stability')", name="ck_eval_results_kind"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["summary_id"],
            ["summaries.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("eval_results", schema=None) as batch_op:
        batch_op.create_index("ix_eval_results_run_kind", ["run_id", "kind"], unique=False)

    op.create_table(
        "verdicts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("summary_id", sa.Integer(), nullable=False),
        sa.Column("verdict", sa.String(length=10), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("verdict in ('ok', 'wrong')", name="ck_verdicts_verdict"),
        sa.ForeignKeyConstraint(
            ["summary_id"],
            ["summaries.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("summary_id"),
    )
    for statement in FTS_DDL:
        op.execute(statement)


def downgrade() -> None:
    for statement in FTS_DROP:
        op.execute(statement)
    op.drop_table("verdicts")
    with op.batch_alter_table("eval_results", schema=None) as batch_op:
        batch_op.drop_index("ix_eval_results_run_kind")

    op.drop_table("eval_results")
    with op.batch_alter_table("summaries", schema=None) as batch_op:
        batch_op.drop_index("ix_summaries_run_rank")

    op.drop_table("summaries")
    with op.batch_alter_table("run_steps", schema=None) as batch_op:
        batch_op.drop_index("ix_run_steps_run")

    op.drop_table("run_steps")
    with op.batch_alter_table("articles", schema=None) as batch_op:
        batch_op.drop_index("ix_articles_published_at")
        batch_op.drop_index("ix_articles_fetched_at")
        batch_op.drop_index("ix_articles_dup_of")

    op.drop_table("articles")
    op.drop_table("sources")
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_index("ix_runs_started_at")

    op.drop_table("runs")
    op.drop_table("daily_counters")
