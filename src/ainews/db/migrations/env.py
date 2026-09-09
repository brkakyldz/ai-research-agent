"""Alembic's environment, driven two ways.

From a terminal (`alembic upgrade head`, `alembic revision --autogenerate`) it
builds its own async engine from `DATABASE_URL`. From the application it is
handed a live connection through `config.attributes`, so a migration at startup
runs inside the engine the app already has rather than opening a second one
against a SQLite file the first is holding.

`render_as_batch` is the setting that matters. SQLite cannot alter a column or
drop a constraint in place; batch mode has Alembic build the new table, copy the
rows and swap it. Without it a migration that changes a unique constraint - the
one Phase 4 of the V2 plan needs - is not expressible at all.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

from ainews.config import get_settings
from ainews.db.migrate import is_ours
from ainews.db.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def _render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Render this project's own column types by name, with their import.

    Autogenerate writes a fully qualified `ainews.db.models.UTCDateTime()` into
    the migration and does not import the module it just named, so every
    generated file that touches a timestamp is broken until someone notices.
    Returning the bare name and registering the import fixes it once instead of
    by hand each time.
    """
    if type_ == "type" and type(obj).__module__.startswith("ainews."):
        name = type(obj).__name__
        autogen_context.imports.add(f"from {type(obj).__module__} import {name}")  # type: ignore[attr-defined]
        return f"{name}()"
    return False


def _configure(**kwargs: object) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=True,
        render_item=_render_item,
        # Without this, autogenerate proposes dropping the FTS5 index and
        # its shadow tables on every revision, because they are in the
        # database and not in the models. `migrate.is_ours` says why.
        include_name=lambda name, type_, _parents: is_ours(name, type_),
        # A column whose Python type changed should show up in an autogenerate
        # diff. SQLite's type affinity makes this noisier than on other
        # backends, which is why it is off by default; the noise is worth the
        # one case where a model and its table have silently drifted.
        compare_type=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
            await connection.commit()
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    asyncio.run(_run_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
