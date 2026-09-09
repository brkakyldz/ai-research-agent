"""One kind for a bulletin run, and one selector that finds it.

`manual` distinguished a pressed digest from a scheduled one and stopped meaning
anything the day ADR 0015 took the clock off. It was not merely spare: the
button and the CLI both wrote `manual` while the rail's archive badge counted
`digest`, so the badge read 1 over an archive listing 3, and `/runs/status` read
`manual` only, so a resumed run never reported its error there. ADR 0026.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ainews.config import Settings
from ainews.db import Bulletin, Run, bulletin_runs
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
    two queries. They are one query now - and the list is bulletins, so a run
    that produced nothing cannot be counted into it at all."""
    session.add_all(
        [
            Bulletin(day="2026-09-08", language="tr", version=1, est_cost_usd=0.0),
            Bulletin(day="2026-09-08", language="en", version=1, est_cost_usd=0.0),
            Run(kind="digest", language="tr", status="error"),  # nothing published
            Run(kind="collect", language="tr", status="ok"),
        ]
    )
    await session.commit()

    listed = await queries.bulletins_page(session, limit=30)
    assert await queries.count_archive(session) == len(listed) == 2


async def test_a_collect_is_never_a_bulletin(session: AsyncSession, engine: AsyncEngine) -> None:
    session.add(Run(kind="collect", language="tr", status="ok", n_collected=40))
    await session.commit()
    assert (await session.execute(bulletin_runs())).scalars().all() == []
