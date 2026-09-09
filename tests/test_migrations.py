"""What replaces `create_all` having been correct by construction.

`Base.metadata.create_all` could not disagree with the models: it *was* the
models. A migration chain can, and the way it goes wrong is quiet - a column
added to a model and not to a revision works on every developer's database,
because theirs was created from the models, and fails on the one archive that
matters, which was created from the revisions.

So the guarantee is asserted rather than assumed: a database built by running
every migration from empty must have the schema the models describe, with
nothing left over in an autogenerate diff. That test is the reason the fast
path was removed rather than kept beside the slow one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ainews.db.migrate import BASELINE, alembic_config, current_revision, head_revision
from ainews.db.models import Base
from ainews.db.schema import init_db


def _diff(connection: Connection) -> list[object]:
    context = MigrationContext.configure(
        connection,
        opts={"compare_type": True, "render_as_batch": True},
    )
    # The FTS5 virtual table and `alembic_version` are in the database and not
    # in the models, by design. Everything else being equal is the claim.
    ignored = {"summaries_fts", "alembic_version"}
    ignored |= {f"summaries_fts_{suffix}" for suffix in ("data", "idx", "docsize", "config")}
    return [
        diff
        for diff in compare_metadata(context, Base.metadata)
        if not (isinstance(diff, tuple) and len(diff) > 1 and str(diff[1]) in ignored)
    ]


async def test_a_migrated_database_matches_the_models(engine: AsyncEngine) -> None:
    """The whole point. `init_db` ran the chain; nothing should be outstanding."""
    async with engine.connect() as connection:
        outstanding = await connection.run_sync(_diff)
    assert outstanding == [], f"models and migrations disagree: {outstanding}"


async def test_init_db_leaves_the_database_at_head(engine: AsyncEngine) -> None:
    assert await current_revision(engine) == head_revision()


async def test_init_db_is_idempotent(engine: AsyncEngine) -> None:
    """It runs on every start, and every entry point calls it."""
    await init_db(engine)
    await init_db(engine)
    assert await current_revision(engine) == head_revision()


async def test_an_archive_from_before_migrations_is_stamped_not_rebuilt(
    tmp_path: Path,
) -> None:
    """The upgrade path for the one database that already exists.

    A file created by `create_all` has every table and no version stamp.
    Applying the baseline to it would try to create tables that are there; the
    truthful move is to record that it is already at the baseline. This builds
    that shape the way it was really built - from the models - and then asks
    `init_db` to take it over.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'archive.db'}"
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        assert await current_revision(engine) is None

        await init_db(engine)

        assert await current_revision(engine) == head_revision()
        # And the rows a real archive would hold are still reachable: stamping
        # touches the version table and nothing else.
        async with engine.begin() as connection:
            names = {
                row[0]
                for row in (
                    await connection.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table'")
                    )
                ).all()
            }
        assert {"runs", "summaries", "articles", "alembic_version"} <= names
    finally:
        await engine.dispose()


async def test_a_stamped_archive_keeps_its_rows(tmp_path: Path) -> None:
    """The failure this guards against is data loss, so it is worth stating with
    a row rather than with a table name."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'archive.db'}"
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(
                text(
                    "INSERT INTO runs (id, kind, language, status, started_at, n_collected,"
                    " n_new, n_summarized, tokens_in, tokens_out, est_cost_usd)"
                    " VALUES ('abc', 'digest', 'tr', 'ok', '2026-09-06T10:00:00', 0, 0, 0, 0, 0, 0)"
                )
            )

        await init_db(engine)

        async with engine.begin() as connection:
            kept = (await connection.execute(text("SELECT id FROM runs"))).scalar_one()
        assert kept == "abc"
    finally:
        await engine.dispose()


def test_the_baseline_is_on_disk_and_is_where_stamping_points() -> None:
    """`BASELINE` names a revision an archive is declared to be at. A typo in it
    would stamp a database at a revision that does not exist, and the next
    upgrade would fail with nothing to explain it."""
    from alembic.script import ScriptDirectory

    scripts = ScriptDirectory.from_config(alembic_config())
    assert scripts.get_revision(BASELINE) is not None


def test_every_revision_defines_both_directions() -> None:
    """A downgrade that is deliberately a no-op is fine and says why; a revision
    with no `downgrade` at all is one nobody thought about reversing."""
    from alembic.script import ScriptDirectory

    scripts = ScriptDirectory.from_config(alembic_config())
    for revision in scripts.walk_revisions():
        source = Path(revision.path).read_text(encoding="utf-8")
        assert "def upgrade()" in source, f"{revision.revision} has no upgrade"
        assert "def downgrade()" in source, f"{revision.revision} has no downgrade"


def test_the_revisions_are_one_chain() -> None:
    """One author, one line of history. A branch point in a repository with a
    single database means two revisions were written from the same parent and
    one of them has never run anywhere."""
    from alembic.script import ScriptDirectory

    scripts = ScriptDirectory.from_config(alembic_config())
    assert len(list(scripts.get_heads())) == 1
    assert len(list(scripts.get_bases())) == 1


@pytest.mark.parametrize("table", ["summaries_fts"])
async def test_the_migration_creates_the_search_index(engine: AsyncEngine, table: str) -> None:
    """FTS5 moved out of application code and into the baseline, because it is
    part of the schema at that revision."""
    async with engine.begin() as connection:
        found = (
            await connection.execute(
                text("SELECT name FROM sqlite_master WHERE name = :name"), {"name": table}
            )
        ).scalar_one_or_none()
    assert found == table
