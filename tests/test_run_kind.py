"""One kind for a bulletin run, and one selector that finds it.

`manual` distinguished a pressed digest from a scheduled one and stopped meaning
anything the day ADR 0015 took the clock off. It was not merely spare: the
button and the CLI both wrote `manual` while the rail's archive badge counted
`digest`, so the badge read 1 over an archive listing 3, and `/runs/status` read
`manual` only, so a resumed run never reported its error there. ADR 0026.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ainews.config import Settings
from ainews.db import Run, bulletin_runs, init_db
from ainews.db.models import RunKind
from ainews.web import queries


def test_there_are_two_kinds_of_run() -> None:
    assert set(RunKind.__args__) == {"collect", "digest"}


def test_the_cli_has_no_mode_flag() -> None:
    """`--mode` chose between two words that now mean the same thing."""
    from ainews.cli import main

    with pytest.raises(SystemExit):
        main(["digest", "--mode", "manual"])


async def test_a_press_and_a_terminal_run_write_the_same_row(
    settings: Settings, engine: AsyncEngine, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ainews.pipeline import runner

    async def _quick(run_id: str, initial: object, s: Settings) -> None:
        return None

    monkeypatch.setattr(runner, "_invoke", _quick)
    run_id = await runner.run_digest(language="tr", settings=settings)
    assert (await session.get(Run, run_id)).kind == "digest"


async def test_the_badge_counts_exactly_what_the_archive_lists(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """The visible half of the bug: two numbers about one list, arrived at by
    two queries. They are one query now."""
    session.add_all(
        [
            Run(kind="digest", language="tr", status="ok", n_summarized=15),
            Run(kind="digest", language="en", status="ok", n_summarized=3),
            Run(kind="digest", language="tr", status="error"),  # nothing produced
            Run(kind="collect", language="tr", status="ok"),
        ]
    )
    await session.commit()

    listed = await queries.digest_runs(session, limit=30)
    assert await queries.count_archive(session) == len(listed) == 2


async def test_a_collect_is_never_a_bulletin(session: AsyncSession, engine: AsyncEngine) -> None:
    session.add(Run(kind="collect", language="tr", status="ok", n_collected=40))
    await session.commit()
    assert (await session.execute(bulletin_runs())).scalars().all() == []


async def test_an_existing_archive_of_manual_rows_is_carried_over(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """The data half of the change, now carried by migration 0002.

    A database written before ADR 0026 keeps an archive of runs that every
    reader filters out - a bulletin history that silently emptied. The rewrite
    was an entry in `DATA_FIXUPS`, applied on every start; it is a revision now,
    applied once and recorded.

    The table is rebuilt in the shape a real archive has: the CHECK still admits
    `manual`, because SQLite bakes it into the DDL at creation and no existing
    file has been rebuilt, while the two model columns are present, because the
    code that added them ran on every start from ADR 0025 until migrations
    arrived. A database older than that shape cannot exist on any machine that
    has run this project, and the baseline says so.
    """
    await session.execute(text("DROP TABLE runs"))
    await session.execute(
        text(
            "CREATE TABLE runs (id VARCHAR(32) PRIMARY KEY, kind VARCHAR(20), "
            "language VARCHAR(2), status VARCHAR(20), started_at DATETIME, "
            "finished_at DATETIME, n_collected INTEGER DEFAULT 0, n_new INTEGER DEFAULT 0, "
            "n_summarized INTEGER DEFAULT 0, tokens_in INTEGER DEFAULT 0, "
            "tokens_out INTEGER DEFAULT 0, est_cost_usd FLOAT DEFAULT 0, "
            "model_summarize VARCHAR(60), model_rank VARCHAR(60), "
            "editor_note TEXT, error TEXT)"
        )
    )
    await session.execute(
        text(
            "INSERT INTO runs (id, kind, language, status, started_at, n_summarized) "
            "VALUES ('old1', 'manual', 'tr', 'ok', '2026-09-05 08:00:00', 15)"
        )
    )
    # The version stamp goes too: this is a database that predates migrations.
    await session.execute(text("DROP TABLE IF EXISTS alembic_version"))
    await session.commit()

    await init_db(engine)

    kept = (await session.execute(bulletin_runs())).scalars().all()
    assert [r.id for r in kept] == ["old1"]
