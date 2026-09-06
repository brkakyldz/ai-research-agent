"""Schema creation, including the parts SQLAlchemy will not model.

The ORM owns the seven real tables. The FTS5 index over `summaries` is a virtual
table plus three triggers, which is DDL the ORM has no vocabulary for, so it is
written out here and applied in the same idempotent `init_db()` call.

There is no migration tool in v1 - see `docs/decisions/0005-no-alembic-in-v1.md`.
Adding one later is `alembic init` plus one autogenerate against these models.
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


async def init_db(engine: AsyncEngine) -> None:
    """Create every table, index and trigger. Safe to call on every start."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for statement in FTS_DDL:
            await conn.execute(text(statement))


async def drop_all(engine: AsyncEngine) -> None:
    """Tests only."""
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS summaries_fts"))
        await conn.run_sync(Base.metadata.drop_all)
