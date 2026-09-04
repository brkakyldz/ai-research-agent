"""Async engine and session factory.

SQLite is configured the way a single-writer local application wants it: WAL so
a reading dashboard never blocks a writing pipeline, `busy_timeout` so the one
writer waits instead of raising, and foreign keys actually enforced (SQLite
leaves them off by default).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ainews.config import Settings, get_settings

log = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _apply_pragmas(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        # WAL needs a shared-memory file next to the database, and some
        # filesystems cannot provide one - notably a Windows host directory
        # bind-mounted into a Linux container, where this raises "disk I/O
        # error" and takes the whole application down at startup. That is a
        # terrible error message for its cause, so it is caught and named here.
        # The tool still works without WAL; a reading page can just block behind
        # a writing digest, which for one reader is a pause, not a failure.
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            log.warning(
                "could not enable WAL (%s). The database file is probably on a "
                "filesystem without shared-memory support, such as a Windows "
                "directory bind-mounted into a container - use a named Docker "
                "volume instead (see docs/decisions/0007-named-volume-for-sqlite.md). "
                "Continuing with the default rollback journal.",
                exc,
            )
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
    finally:
        cursor.close()


def create_engine(settings: Settings | None = None, url: str | None = None) -> AsyncEngine:
    settings = settings or get_settings()
    url = url or settings.database_url
    if url.startswith("sqlite") and ":///" in url:
        _, _, tail = url.partition(":///")
        if tail and tail != ":memory:":
            path = Path(tail)
            if not path.is_absolute():
                path = (settings.sqlite_path.parent / path.name).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(url, echo=False, future=True)
    event.listen(engine.sync_engine, "connect", _apply_pragmas)
    return engine


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transaction boundary for pipeline code: commit on success, roll back on error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Read-mostly; routes that write commit explicitly."""
    async with get_sessionmaker()() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def checkpoint_wal(engine: AsyncEngine | None = None) -> None:
    """Fold the WAL back into the main database file - called at shutdown."""
    engine = engine or get_engine()
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
