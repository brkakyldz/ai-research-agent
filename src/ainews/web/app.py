"""FastAPI application factory.

The app owns the database lifecycle (created on start, WAL-checkpointed on
stop), the routes and the templates. The scheduler joins it in M5 - ADR 0004
puts it in this process so the cron path and the "Run now" button call the same
function.
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
from ainews.db.session import checkpoint_wal
from ainews.logging_conf import configure_logging

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

    try:
        yield
    finally:
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
