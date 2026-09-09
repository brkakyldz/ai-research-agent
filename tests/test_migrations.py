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
from alembic import command
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


async def _archive_as_it_was(path: Path) -> AsyncEngine:
    """A database in the shape the real archive is in: every table the baseline
    describes, and no version stamp.

    Built by running the baseline and then removing the stamp, rather than by
    `create_all`, which would build the *current* models - a database that has
    already had every later revision applied to it and would then have them
    applied again.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(lambda c: command.upgrade(alembic_config(c), BASELINE))
    async with engine.begin() as connection:
        await connection.execute(text("DROP TABLE alembic_version"))
    return engine


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

    The archive has every table the baseline describes and no version stamp.
    Applying the baseline to it would try to create tables that are there; the
    truthful move is to record that it is already at that point and carry on.
    """
    engine = await _archive_as_it_was(tmp_path / "archive.db")
    try:
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
    engine = await _archive_as_it_was(tmp_path / "archive.db")
    try:
        async with engine.begin() as connection:
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


async def test_the_manual_run_kind_is_rewritten_on_an_old_archive(tmp_path: Path) -> None:
    """Migration `0002`, and the reason a data revision is worth having.

    `manual` distinguished a pressed digest from a scheduled one and stopped
    meaning anything when ADR 0015 took the clock off (ADR 0026). Rows carrying
    it are filtered out by every reader, so leaving them is a bulletin history
    that silently empties. The rewrite used to be a statement executed on every
    start for the rest of the project's life.
    """
    engine = await _archive_as_it_was(tmp_path / "archive.db")
    try:
        async with engine.begin() as connection:
            # `runs` is rebuilt with the CHECK a real archive still carries.
            # SQLite bakes the constraint into the table at creation, so a file
            # made before ADR 0026 admits `manual` and a file made after it does
            # not - the difference the baseline's docstring accepts rather than
            # repairs. Without this the row cannot be inserted at all, and the
            # revision would be tested against a database that never needed it.
            await connection.execute(text("DROP TABLE runs"))
            await connection.execute(
                text(
                    "CREATE TABLE runs (id VARCHAR(32) PRIMARY KEY, kind VARCHAR(20), "
                    "language VARCHAR(2), status VARCHAR(20), started_at DATETIME, "
                    "finished_at DATETIME, n_collected INTEGER DEFAULT 0, "
                    "n_new INTEGER DEFAULT 0, n_summarized INTEGER DEFAULT 0, "
                    "tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0, "
                    "est_cost_usd FLOAT DEFAULT 0, model_summarize VARCHAR(60), "
                    "model_rank VARCHAR(60), editor_note TEXT, error TEXT, "
                    "CHECK (kind in ('collect', 'digest', 'manual')))"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO runs (id, kind, language, status, started_at, n_summarized)"
                    " VALUES ('old1', 'manual', 'tr', 'ok', '2026-09-05T08:00:00', 15)"
                )
            )

        await init_db(engine)

        async with engine.begin() as connection:
            kind = (
                await connection.execute(text("SELECT kind FROM runs WHERE id = 'old1'"))
            ).scalar_one()
        assert kind == "digest"
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


# -- 0004: the two objects ----------------------------------------------------


async def _archive_with_a_published_run(path: Path, *, extra_summary: bool = False) -> AsyncEngine:
    """A pre-0004 archive holding one digest run that published three stories.

    Built at `0003_provenance` and not at head, because the columns this
    revision reads - `summaries.run_id`, `rank`, `editor_importance` - do not
    exist at head. That is what makes it the only chance to write them down.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(lambda c: command.upgrade(alembic_config(c), "0003_provenance"))
        await connection.execute(
            text(
                "INSERT INTO sources (id, name, url, kind, weight, enabled,"
                " consecutive_failures)"
                " VALUES (1, 'Simon Willison', 'https://sw.net/feed', 'rss', 1.5, 1, 0)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO runs (id, kind, language, status, started_at, n_collected, n_new,"
                " n_summarized, tokens_in, tokens_out, est_cost_usd, model_rank, editor_note)"
                " VALUES ('run1', 'digest', 'tr', 'ok', '2026-09-06 13:00:00', 40, 12, 12,"
                " 9000, 3000, 0.0248, 'gpt-5.6-luna', 'Gunun ozeti.')"
            )
        )
        for n in range(1, 5):
            await connection.execute(
                text(
                    "INSERT INTO articles (id, source_id, title, url, url_canonical, body_text,"
                    " body_source, fetched_at)"
                    " VALUES (:id, 1, :title, :url, :url, 'A body.', 'feed',"
                    " '2026-09-06 12:00:00')"
                ),
                {"id": n, "title": f"Story {n}", "url": f"https://sw.net/{n}"},
            )
            # Three of the four were ranked; the fourth was summarised and left
            # out, which is the row that must not become a bulletin item.
            await connection.execute(
                text(
                    "INSERT INTO summaries (id, article_id, run_id, language, title_local,"
                    " summary, why_it_matters, tags_json, importance, rank, editor_importance,"
                    " created_at)"
                    " VALUES (:id, :id, 'run1', 'tr', :title, 'Ozet metni.', 'Onemi.', '[]',"
                    " 3, :rank, :editor, '2026-09-06 13:05:00')"
                ),
                {
                    "id": n,
                    "title": f"Baslik {n}",
                    "rank": n if n < 4 else None,
                    "editor": 5 if n == 1 else None,
                },
            )
        if extra_summary:
            # A second run, because the old key is `(article_id, run_id,
            # language)` and one run could never hold two summaries of an
            # article. Two runs could, and only a query in `dedupe` stopped it.
            await connection.execute(
                text(
                    "INSERT INTO runs (id, kind, language, status, started_at, n_collected,"
                    " n_new, n_summarized, tokens_in, tokens_out, est_cost_usd)"
                    " VALUES ('run0', 'digest', 'tr', 'ok', '2026-09-06 09:00:00', 1, 1, 1,"
                    " 100, 50, 0.001)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO summaries (id, article_id, run_id, language, title_local,"
                    " summary, why_it_matters, tags_json, importance, created_at)"
                    " VALUES (99, 1, 'run0', 'tr', 'Ikinci baslik', 'x', 'y', '[]', 3,"
                    " '2026-09-06 09:05:00')"
                )
            )
    return engine


async def test_a_published_run_becomes_a_bulletin(tmp_path: Path) -> None:
    """Migration `0004`. What each run put on the front page is recorded in
    `rank` and `editor_importance` and nowhere else, and the same revision drops
    both."""
    engine = await _archive_with_a_published_run(tmp_path / "archive.db")
    try:
        await init_db(engine)

        async with engine.begin() as connection:
            bulletin = (
                await connection.execute(
                    text(
                        "SELECT day, language, version, editor_note, model_rank, run_id,"
                        " est_cost_usd FROM bulletins"
                    )
                )
            ).one()
            items = (
                await connection.execute(
                    text("SELECT summary_id, position, tier FROM bulletin_items ORDER BY position")
                )
            ).all()
        # 13:00 UTC is the same calendar day in every zone the tests run in.
        assert bulletin == ("2026-09-06", "tr", 1, "Gunun ozeti.", "gpt-5.6-luna", "run1", 0.0248)
        # The unranked fourth summary is not in the bulletin. It is still a
        # summary; it was simply not published.
        assert items == [(1, 1, "lead"), (2, 2, "major"), (3, 3, "major")]
    finally:
        await engine.dispose()


async def test_the_summaries_survive_the_rebuild_with_their_text(tmp_path: Path) -> None:
    """The revision drops three columns from a populated table, which on SQLite
    is a copy into a new one. The failure that would hide is silent row loss."""
    engine = await _archive_with_a_published_run(tmp_path / "archive.db")
    try:
        await init_db(engine)

        async with engine.begin() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT id, title_local, relevant, kind, est_cost_usd FROM summaries"
                        " ORDER BY id"
                    )
                )
            ).all()
        assert len(rows) == 4
        assert rows[0] == (1, "Baslik 1", 1, "news", 0.0)
    finally:
        await engine.dispose()


async def test_the_search_index_still_finds_the_rebuilt_rows(tmp_path: Path) -> None:
    """A contentless FTS5 table indexes by rowid and its triggers live on the
    table the rebuild replaces. Left alone, the index survives the copy pointing
    at a table that no longer exists and nothing new is ever indexed."""
    engine = await _archive_with_a_published_run(tmp_path / "archive.db")
    try:
        await init_db(engine)

        async with engine.begin() as connection:
            found = (
                (
                    await connection.execute(
                        text(
                            "SELECT rowid FROM summaries_fts WHERE summaries_fts MATCH 'Baslik'"
                            " ORDER BY rowid"
                        )
                    )
                )
                .scalars()
                .all()
            )
            await connection.execute(
                text(
                    "INSERT INTO summaries (article_id, language, title_local, summary,"
                    " why_it_matters, tags_json, importance, relevant, kind, model, tokens_in,"
                    " tokens_out, est_cost_usd, created_at)"
                    " VALUES (4, 'en', 'Zzzunique headline', 's', 'w', '[]', 3, 1, 'news',"
                    " 'gpt-5.6-luna', 10, 5, 0.001, '2026-09-09 10:00:00')"
                )
            )
            indexed = (
                await connection.execute(
                    text("SELECT count(*) FROM summaries_fts WHERE summaries_fts MATCH 'Zzzunique'")
                )
            ).scalar_one()
        assert found == [1, 2, 3, 4]
        # The triggers are back, so the row written after the rebuild is indexed.
        assert indexed == 1
    finally:
        await engine.dispose()


async def test_the_added_columns_keep_no_default_behind(tmp_path: Path) -> None:
    """The defaults exist to fill the rows already there. The application writes
    every one of these on insert, and a default left in place is a second source
    for a value - the shape of bug where a model name silently becomes ''."""
    engine = await _archive_with_a_published_run(tmp_path / "archive.db")
    try:
        await init_db(engine)

        async with engine.begin() as connection:
            defaults = {
                row[1]: row[4]
                for row in (await connection.execute(text("PRAGMA table_info(summaries)"))).all()
            }
        assert defaults["model"] is None
        assert defaults["relevant"] is None
        assert defaults["est_cost_usd"] is None
    finally:
        await engine.dispose()


async def test_two_summaries_of_one_article_stop_the_upgrade_by_name(tmp_path: Path) -> None:
    """The new key forbids them. Reaching the batch copy first would report a
    constraint name and no row, on a half-migrated database.

    `RuntimeError` and not the revision's own class: a module whose name starts
    with a digit is not importable by name, and a test that reached into it with
    `importlib` would be asserting on the plumbing rather than on the message an
    operator reads.
    """
    engine = await _archive_with_a_published_run(tmp_path / "archive.db", extra_summary=True)
    try:
        with pytest.raises(RuntimeError) as raised:
            await init_db(engine)
        assert "article 1 in tr" in str(raised.value)
        # Nothing was rebuilt: the check runs before the first write.
        async with engine.begin() as connection:
            assert (
                await connection.execute(
                    text("SELECT count(*) FROM sqlite_master WHERE name = 'bulletins'")
                )
            ).scalar_one() == 0
    finally:
        await engine.dispose()
