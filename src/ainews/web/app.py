"""FastAPI application factory.

The app owns three things: the database lifecycle (created on start,
WAL-checkpointed on stop), the seeded feed list, and the scheduler - which lives
here rather than in its own process so that the cron path and the "Run now"
button call the same function (ADR 0004).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ainews.config import Settings, get_settings
from ainews.db import dispose_engine, get_engine, init_db
from ainews.db.session import checkpoint_wal, session_scope
from ainews.logging_conf import configure_logging
from ainews.scheduler import start_scheduler
from ainews.sources.seed import sync_sources

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
TEMPLATE_DIR = WEB_DIR / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings.log_level)
    await init_db(get_engine())
    log.info("database ready at %s", settings.sqlite_path)

    # Seeding on every start, not only the first: a feed added to feeds.yaml in a
    # later version has to reach an existing installation, and the sync is
    # additive so nothing the operator changed is touched.
    async with session_scope() as session:
        await sync_sources(session)

    scheduler = None
    if settings.scheduler_enabled:
        scheduler = start_scheduler(settings)
    else:
        log.info("scheduler disabled by configuration")
    app.state.scheduler = scheduler

    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        await checkpoint_wal()
        await dispose_engine()
        log.info("shutdown complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="AI News Digest",
        version="0.1.0",
        docs_url="/api/docs" if settings.environment == "development" else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    from ainews.web.routes import register_routes

    register_routes(app)
    return app


app = create_app()
