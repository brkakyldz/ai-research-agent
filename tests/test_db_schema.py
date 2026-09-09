from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ainews.db import Article, Source, Summary
from ainews.db.models import Run
from ainews.db.schema import init_db


async def test_init_db_is_idempotent(engine: AsyncEngine) -> None:
    """It runs on every start, so running it twice must be a no-op."""
    await init_db(engine)
    async with engine.begin() as conn:
        rows = (await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))).all()
    names = {r[0] for r in rows}
    assert {"sources", "articles", "runs", "summaries", "daily_counters"} <= names


async def test_wal_and_foreign_keys_are_on(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        journal = (await conn.execute(text("PRAGMA journal_mode"))).scalar_one()
        fks = (await conn.execute(text("PRAGMA foreign_keys"))).scalar_one()
    assert journal.lower() == "wal"
    assert fks == 1


async def test_canonical_url_is_unique(session: AsyncSession) -> None:
    src = Source(name="Test", url="https://example.com/feed.xml")
    session.add(src)
    await session.flush()
    session.add(
        Article(source_id=src.id, url_canonical="https://example.com/a", url="x", title="A")
    )
    await session.flush()
    session.add(
        Article(source_id=src.id, url_canonical="https://example.com/a", url="y", title="A again")
    )
    try:
        await session.flush()
    except IntegrityError:
        return
    raise AssertionError("duplicate canonical URL was accepted")


async def test_fts_index_follows_summary_writes(session: AsyncSession) -> None:
    src = Source(name="Test", url="https://example.com/feed.xml")
    session.add(src)
    await session.flush()
    art = Article(source_id=src.id, url_canonical="https://example.com/x", url="x", title="T")
    run = Run(kind="digest", language="tr")
    session.add_all([art, run])
    await session.flush()
    session.add(
        Summary(
            article_id=art.id,
            run_id=run.id,
            language="tr",
            title_local="Yeni bir dil modeli duyuruldu",
            summary="Sirket bugun yeni bir model yayinladi.",
            why_it_matters="Fiyat rekabeti degisiyor.",
            importance=4,
        )
    )
    await session.commit()

    hit = (
        await session.execute(
            text("SELECT rowid FROM summaries_fts WHERE summaries_fts MATCH :q"),
            {"q": "model"},
        )
    ).all()
    assert len(hit) == 1

    # And the delete trigger has to unindex it, or search grows ghosts.
    await session.execute(text("DELETE FROM summaries"))
    await session.commit()
    after = (
        await session.execute(
            text("SELECT rowid FROM summaries_fts WHERE summaries_fts MATCH :q"),
            {"q": "model"},
        )
    ).all()
    assert after == []


def test_a_filesystem_without_shared_memory_degrades_instead_of_crashing() -> None:
    """A Windows directory bind-mounted into a Linux container cannot do WAL.

    Left unhandled, `PRAGMA journal_mode=WAL` raises "disk I/O error" inside a
    connect listener, so the application dies at startup with a message naming
    neither the cause nor the fix (ADR 0007). The pragma has to be survivable,
    and the pragmas after it still have to be applied - foreign keys are a
    correctness feature, not a performance one.
    """
    import sqlite3

    from ainews.db.session import _apply_pragmas

    executed: list[str] = []

    class Cursor:
        def execute(self, sql: str) -> None:
            executed.append(sql)
            if "journal_mode=WAL" in sql:
                raise sqlite3.OperationalError("disk I/O error")

        def close(self) -> None:
            pass

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

    _apply_pragmas(Connection(), None)  # must not raise

    assert any("journal_mode=WAL" in sql for sql in executed)
    assert any("foreign_keys=ON" in sql for sql in executed)
    assert any("busy_timeout" in sql for sql in executed)
