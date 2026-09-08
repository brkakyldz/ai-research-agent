"""Shared fixtures.

Every test runs against its own SQLite file in a tmp directory, never the real
`data/app.db`, and never against the developer's real API keys: the fixtures
below overwrite both before the settings cache is built.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ainews import db as db_pkg
from ainews.config import Settings, get_settings
from ainews.db import create_engine, init_db
from ainews.db.session import dispose_engine


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No real keys, no real database, no scheduler - in every test."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_file.as_posix()}")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    # Pinned off, not merely defaulted off: a developer who has turned tracing
    # on for real runs would otherwise have the suite post spans to Phoenix.
    monkeypatch.setenv("PHOENIX_ENABLED", "false")
    monkeypatch.setenv("ENVIRONMENT", "development")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _release_the_run_slot() -> Iterator[None]:
    """The run slot is module-level state, so a test that takes it and fails
    would otherwise make every later test think a run is in flight."""
    from ainews.pipeline import runner

    runner._slot_holders.clear()
    yield
    runner._slot_holders.clear()


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest_asyncio.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(settings)
    await init_db(eng)
    # Point the module-level singletons at this engine so application code
    # (which resolves them lazily) reaches the test database too.
    db_pkg.session._engine = eng
    db_pkg.session._sessionmaker = async_sessionmaker(eng, expire_on_commit=False)
    try:
        yield eng
    finally:
        await dispose_engine()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.fixture
def env_free_of_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    os.environ.pop("TAVILY_API_KEY", None)
    get_settings.cache_clear()
