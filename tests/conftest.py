"""Shared fixtures.

Every test runs against its own SQLite file in a tmp directory, never the real
`data/app.db`, and never against the developer's real API keys: the fixtures
below overwrite both before the settings cache is built.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ainews import db as db_pkg
from ainews.config import Settings, get_settings
from ainews.db import Article, Run, Source, Summary, create_engine, init_db
from ainews.db.session import dispose_engine
from ainews.web.app import create_app


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


@pytest.fixture
def client(settings: Settings, engine: AsyncEngine) -> TestClient:
    """No lifespan: the `engine` fixture already built the schema, and running it
    again would point the app at a second engine.

    It was five identical copies across the page test files until 2026-09-08.
    """
    return TestClient(create_app(settings))


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s


TITLES = [
    ("OpenAI ucuz bir model duyurdu", 5),
    ("Avrupa yapay zeka yasasi icin rehber yayimladi", 4),
    ("Bir robotik girisimi yatirim aldi", 3),
    ("Kucuk bir kutuphane surum cikardi", 2),
    ("Topluluk derlemesi paylasildi", 1),
]


@pytest.fixture
async def digest(session: AsyncSession) -> Run:
    """One finished Turkish digest: three ranked stories and two below the fold.

    Here rather than in a page test file because three of them want it: the
    pages, the shell and the press. It was one 1,307-line file until 2026-09-08
    and the fixture came out of the split with it.
    """
    src = Source(name="OpenAI", url="https://openai.com/news/rss.xml", weight=2.0)
    session.add(src)
    await session.flush()

    run = Run(
        kind="digest",
        language="tr",
        status="ok",
        n_summarized=len(TITLES),
        n_new=len(TITLES),
        est_cost_usd=0.0421,
        tokens_in=1000,
        tokens_out=400,
        editor_note="Gunun ortak konusu fiyat degil olcum.",
        finished_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()

    for index, (title, importance) in enumerate(TITLES):
        art = Article(
            source_id=src.id,
            title=title,
            url=f"https://openai.com/news/{index}",
            url_canonical=f"https://openai.com/news/{index}",
            published_at=datetime.now(UTC) - timedelta(hours=index + 1),
        )
        session.add(art)
        await session.flush()
        session.add(
            Summary(
                article_id=art.id,
                run_id=run.id,
                language="tr",
                title_local=title,
                summary=f"Ozet metni {index}. Ikinci cumle. Ucuncu cumle.",
                why_it_matters="Bu yuzden onemli.",
                tags_json=json.dumps(["openai", "models"] if index < 2 else ["policy"]),
                importance=importance,
                rank=index + 1 if index < 3 else None,
            )
        )
    await session.commit()
    return run


@pytest.fixture
def env_free_of_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    os.environ.pop("TAVILY_API_KEY", None)
    get_settings.cache_clear()
