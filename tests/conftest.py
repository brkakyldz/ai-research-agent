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
from ainews.db import (
    Article,
    Bulletin,
    BulletinItem,
    Run,
    Source,
    Summary,
    create_engine,
    init_db,
)
from ainews.db.session import dispose_engine
from ainews.web.app import create_app
from ainews.web.format import to_local


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

    Here rather than in each page test file, where it was five identical copies.
    """
    return TestClient(create_app(settings))


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s


TITLES = [
    ("OpenAI ucuz bir model duyurdu", 5, "lead"),
    ("Avrupa yapay zeka yasasi icin rehber yayimladi", 4, "major"),
    ("Bir robotik girisimi yatirim aldi", 3, "notable"),
    ("Kucuk bir kutuphane surum cikardi", 2, None),
    ("Topluluk derlemesi paylasildi", 1, None),
]


@pytest_asyncio.fixture
async def digest(session: AsyncSession) -> Bulletin:
    """One published Turkish bulletin: three stories, and two the editor left out.

    A `Bulletin` and not a `Run`, because a bulletin is what a page shows (ADR
    0030). The run is written too - the archive page does not read it, but
    `/runs` does, and a bulletin with no press behind it is a state the
    application cannot produce.

    Here rather than in a page test file because three of them want it: the
    pages, the shell and the press, which were one 1,307-line file before the
    split that this fixture came out of.
    """
    src = Source(name="OpenAI", url="https://openai.com/news/rss.xml", weight=2.0)
    session.add(src)
    await session.flush()

    now = datetime.now(UTC)
    run = Run(
        kind="digest",
        language="tr",
        status="ok",
        n_summarized=len(TITLES),
        n_new=len(TITLES),
        est_cost_usd=0.0421,
        tokens_in=1000,
        tokens_out=400,
        finished_at=now,
    )
    session.add(run)
    await session.flush()

    bulletin = Bulletin(
        day=to_local(now).date().isoformat(),  # type: ignore[union-attr]
        language="tr",
        version=1,
        editor_note="Gunun ortak konusu fiyat degil olcum.",
        model_rank="gpt-5.6-luna",
        agreement=0.82,
        est_cost_usd=0.004,
        run_id=run.id,
    )
    session.add(bulletin)
    await session.flush()

    position = 0
    for index, (title, importance, tier) in enumerate(TITLES):
        art = Article(
            source_id=src.id,
            title=title,
            url=f"https://openai.com/news/{index}",
            url_canonical=f"https://openai.com/news/{index}",
            published_at=now - timedelta(hours=index + 1),
        )
        session.add(art)
        await session.flush()
        summary = Summary(
            article_id=art.id,
            language="tr",
            title_local=title,
            summary=f"Ozet metni {index}. Ikinci cumle. Ucuncu cumle.",
            why_it_matters="Bu yuzden onemli.",
            tags_json=json.dumps(["openai", "models"] if index < 2 else ["policy"]),
            importance=importance,
            model="gpt-5.6-luna",
            tokens_in=1400,
            tokens_out=500,
            est_cost_usd=0.0008,
        )
        session.add(summary)
        await session.flush()
        if tier is not None:
            position += 1
            session.add(
                BulletinItem(
                    bulletin_id=bulletin.id,
                    summary_id=summary.id,
                    position=position,
                    tier=tier,
                    reason="Gunun en agir haberi." if position == 1 else None,
                )
            )
    await session.commit()
    return bulletin


@pytest.fixture
def env_free_of_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    os.environ.pop("TAVILY_API_KEY", None)
    get_settings.cache_clear()
