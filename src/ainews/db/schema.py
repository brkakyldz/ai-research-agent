"""Bringing a database up to the schema the code expects.

Since ADR 0028 that is one sentence: run the migrations. `create_all` is gone,
and with it `ADDED_COLUMNS` and `DATA_FIXUPS` - the two narrow amendments ADR
0005 allowed so that a live archive could gain a nullable column and lose a
retired value without a migration tool. They were the right answer twice and the
wrong answer the third time: Phase 4 of the V2 plan replaces a unique constraint
on `summaries`, which SQLite can only do by rebuilding the table, and neither
amendment can express that.

`init_db` stays the one call every entry point makes at startup, and stays
idempotent. What changed is what it does: an archive with no version stamp is
stamped at the baseline and carried forward, a stamped one is upgraded, and a
fresh file gets every revision applied in order. A test asserts that the last of
those three arrives at the same schema the models describe - which is the
guarantee `create_all` used to provide by construction and now has to be
checked.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ainews.db.migrate import upgrade_to_head
from ainews.db.models import Base

# The FTS5 index is created by the baseline migration, which owns the schema.
# These are the drop statements `drop_all` needs, and they are here rather than
# imported from the migration because a test tearing a database down should not
# depend on which revision created what.
FTS_DROP = [
    "DROP TRIGGER IF EXISTS summaries_fts_au",
    "DROP TRIGGER IF EXISTS summaries_fts_ad",
    "DROP TRIGGER IF EXISTS summaries_fts_ai",
    "DROP TABLE IF EXISTS summaries_fts",
]


async def init_db(engine: AsyncEngine) -> None:
    """Bring the database to the current revision. Safe on every start."""
    await upgrade_to_head(engine)


async def drop_all(engine: AsyncEngine) -> None:
    """Tests only."""
    async with engine.begin() as conn:
        for statement in FTS_DROP:
            await conn.execute(text(statement))
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await conn.run_sync(Base.metadata.drop_all)
