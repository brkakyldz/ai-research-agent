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
    async with engine.begin() as connection:
        await connection.run_sync(_bring_to_head)


async def current_revision(engine: AsyncEngine) -> str | None:
    async with engine.connect() as connection:
        return await connection.run_sync(_current)
