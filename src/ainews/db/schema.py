"""Schema creation, including the parts SQLAlchemy will not model.

The ORM owns the real tables. The FTS5 index over `summaries` is a virtual table
plus three triggers, which is DDL the ORM has no vocabulary for, so it is written
out here and applied in the same idempotent `init_db()` call.

There is no migration tool in v1 - see `docs/decisions/0005-no-alembic-in-v1.md`.
`create_all` adds tables and never columns, which held until the archive was
worth keeping and a column was needed on it (ADR 0025). `ADDED_COLUMNS` is the
narrow answer: additive, nullable, applied only when `PRAGMA table_info` says
the column is missing, and listed here so the whole history of the schema
outside `create_all` is one list. `DATA_FIXUPS` is the second narrow answer
(ADR 0026): idempotent one-statement updates for a value the code has stopped
writing, so an existing archive does not keep rows in a shape nothing reads.
Adding anything wider is still `alembic init` plus one autogenerate against
these models.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ainews.db.models import Base

# `content=''` makes this a contentless FTS5 table: the text lives once, in
# `summaries`, and the index stores only the terms. The triggers keep the two in
# step; `rowid` is the summary id, so a hit joins straight back.
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

# (table, column, SQL type). Every entry is a column that `create_all` created
# on a fresh database and that a database created earlier lacks. Nullable, no
# default, no constraint - the three things SQLite's `ADD COLUMN` and a live
# archive both tolerate.
ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("summaries", "editor_importance", "INTEGER"),  # ADR 0025, 2026-09-08
    ("runs", "model_summarize", "VARCHAR(60)"),  # 2026-09-08
    ("runs", "model_rank", "VARCHAR(60)"),  # 2026-09-08
]

# The other half of the same amendment: a value the code no longer writes and no
# longer reads, left on rows an existing archive still holds. Idempotent by
# construction - the second run matches nothing - and each entry has to stay
# harmless on a fresh database, where it also matches nothing. Same bar as
# `ADDED_COLUMNS`: no new table, no dropped column, no rewritten row that a
# reader would notice as different data.
DATA_FIXUPS: list[str] = [
    # ADR 0026, 2026-09-08. `manual` distinguished a pressed digest from a
    # scheduled one and stopped meaning anything when ADR 0015 took the clock
    # off. Leaving the rows behind would have kept every reader filtering for a
    # kind that is no longer written.
    "UPDATE runs SET kind = 'digest' WHERE kind = 'manual'",
]


async def init_db(engine: AsyncEngine) -> None:
    """Create every table, index and trigger. Safe to call on every start."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for statement in FTS_DDL:
            await conn.execute(text(statement))
        for table, column, sql_type in ADDED_COLUMNS:
            present = {
                row[1] for row in (await conn.execute(text(f"PRAGMA table_info({table})"))).all()
            }
            if column not in present:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"))
        for statement in DATA_FIXUPS:
            await conn.execute(text(statement))


async def drop_all(engine: AsyncEngine) -> None:
    """Tests only."""
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS summaries_fts"))
        await conn.run_sync(Base.metadata.drop_all)
