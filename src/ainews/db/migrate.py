"""Running Alembic from inside the application.

The command line is the other way in (`alembic upgrade head`), and it reaches
the same scripts through `alembic.ini`. This module exists so that starting the
app is enough: a single-user tool that asks its operator to remember a migration
step before the first run has traded one kind of breakage for another.

Nothing here opens a connection of its own. Every entry point takes the engine
the caller already has and hands Alembic a connection from it, because SQLite
holds a write lock per file and a second engine against the same file during
startup is a `database is locked` waiting to happen.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, inspect
from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# The revision an archive created before Alembic is stamped at. Every table in
# it already existed; see the migration's own docstring.
BASELINE = "0001_baseline"

# The FTS5 index and the four shadow tables SQLite creates beside it. They are
# in the database and deliberately not in the models - a virtual table is DDL
# SQLAlchemy has no vocabulary for - so autogenerate sees them as tables to
# drop and writes `op.drop_table('summaries_fts')` into the next revision
# anybody generates. That would delete the search index on the next upgrade,
# quietly, as a side effect of an unrelated change.
FTS_TABLES = frozenset(
    {
        "summaries_fts",
        "summaries_fts_data",
        "summaries_fts_idx",
        "summaries_fts_docsize",
        "summaries_fts_config",
    }
)


def is_ours(name: str | None, type_: str) -> bool:
    """Whether autogenerate should compare this object against the models."""
    return not (type_ == "table" and name in FTS_TABLES)


def alembic_config(connection: Connection | None = None) -> Config:
    """A Config built in code rather than read from `alembic.ini`.

    The ini file is for the command line. Building it here keeps the one thing
    that would otherwise be written twice - where the scripts live - in one
    place, and lets a live connection be passed through `attributes`, which is
    what `migrations/env.py` looks for.
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def head_revision() -> str:
    """The newest revision on disk. Read from the scripts, never hard-coded."""
    return ScriptDirectory.from_config(alembic_config()).get_current_head() or BASELINE


def _current(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def _has_tables(connection: Connection) -> bool:
    """Whether this database predates migrations: it has our tables and no
    version stamp. `runs` is the witness because every install has one."""
    return "runs" in inspect(connection).get_table_names()


def _bring_to_head(connection: Connection) -> None:
    config = alembic_config(connection)
    if _current(connection) is None and _has_tables(connection):
        # An archive built by `create_all` and amended by hand. Applying the
        # baseline would try to create tables that exist; the truthful move is
        # to record where it already is and carry on from there.
        log.info("existing database has no version stamp; stamping at %s", BASELINE)
        command.stamp(config, BASELINE)
    command.upgrade(config, "head")


async def upgrade_to_head(engine: AsyncEngine) -> None:
    """Bring the database to head with foreign keys off for the duration.

    Alembic rebuilds a SQLite table by copying it, dropping the original and
    renaming the copy back over it. Under `PRAGMA foreign_keys=ON` - which
    `session.py` sets on every application connection - the drop is refused the
    moment another table holds a row pointing at it, so a revision that passes
    on an empty database fails on the operator's: `verdicts` and `eval_results`
    both reference `summaries`, and 0004 rebuilds it.

    `defer_foreign_keys` is not the lighter alternative it looks like. It moves
    the check to the commit but still *counts* the drop's implicit deletes, and
    renaming the copy back into place inserts nothing, so the count never comes
    down and the commit fails with the table whole and every reference intact.

    Off is therefore the only setting that works, and it has to be set before
    the transaction opens, because inside one the pragma is silently a no-op.
    What replaces the enforcement is `PRAGMA foreign_key_check` afterwards: a
    migration that really did strand a row is then loud rather than committed
    in silence, which is the trade the pragma would otherwise make for us.
    """
    async with engine.connect() as connection:
        # A statement autobegins a SQLAlchemy transaction even when the SQLite
        # driver has emitted no BEGIN of its own; the rollback clears that
        # bookkeeping so `begin()` below is allowed. It does not touch the
        # pragma, which is connection state rather than transaction state.
        await connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        await connection.rollback()
        try:
            async with connection.begin():
                await connection.run_sync(_bring_to_head)
            stranded = (await connection.exec_driver_sql("PRAGMA foreign_key_check")).fetchall()
        finally:
            await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            await connection.rollback()
        if stranded:
            raise RuntimeError(
                "the migration left rows pointing at parents that no longer exist: "
                + "; ".join(
                    f"{table}.rowid={rowid} -> {parent}" for table, rowid, parent, _ in stranded
                )
            )


async def current_revision(engine: AsyncEngine) -> str | None:
    async with engine.connect() as connection:
        return await connection.run_sync(_current)
