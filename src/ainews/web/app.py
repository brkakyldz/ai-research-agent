"""FastAPI application factory.

The app owns three things: the database lifecycle (created on start,
WAL-checkpointed on stop), the seeded feed list, and the scheduler - which lives
here rather than in its own process so that the timed path and the button call
the same function (ADR 0004). Since ADR 0015 the scheduler winds one job, the
feed poll; the digest is started by a person from `/runs`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ainews import __version__
from ainews.config import Settings, get_settings
from ainews.db import dispose_engine, get_engine, init_db
from ainews.db.session import checkpoint_wal, session_scope
from ainews.demo import seed_demo
from ainews.demo.seed import DemoNotEmpty
from ainews.logging_conf import configure_logging
from ainews.observability import enable_tracing
from ainews.pipeline.api import reconcile_orphaned_runs
from ainews.scheduler import start_scheduler
from ainews.sources.seed import sync_sources
from ainews.web.format import build_templates
from ainews.web.routes import register_routes
from ainews.web.security import same_origin_only

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
TEMPLATE_DIR = WEB_DIR / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings.log_level)
    enable_tracing(settings)
    await init_db(get_engine())
    log.info("database ready at %s", settings.sqlite_path)

    # Seeding on every start, not only the first: a feed added to feeds.yaml in a
    # later version has to reach an existing installation, and the sync is
    # additive so nothing the operator changed is touched.
    async with session_scope() as session:
        await sync_sources(session)

    # A fresh clone with `DEMO_MODE=true` and no API key: load the recorded day
    # so `docker compose up` shows the application rather than an empty page and
    # a button nobody can press. Only into an empty archive - `seed_demo` refuses
    # otherwise, and mixing a recording into a real archive is the one mistake
    # here that cannot be undone from the interface.
    if settings.demo_mode:
        async with session_scope() as session:
            try:
                await seed_demo(session)
            except (DemoNotEmpty, LookupError) as exc:
                log.info("demo seed skipped: %s", exc)

    # Before the scheduler winds, and before a request can be served: a run row
    # left `running` by a killed process is a run nothing will ever close, and
    # the advice block on `/runs` reads the state of the last one.
    await reconcile_orphaned_runs()

    scheduler = None
    if settings.demo_mode:
        # A demo polls no feeds. The one scheduled job left is the three-hourly
        # collect (ADR 0015), and on a reviewer's machine it would make network
        # calls nobody asked for and file live articles beside a recording -
        # after which the page is half recorded and half real, and nothing on it
        # says which half is which.
        log.info("scheduler off: this is a demo, and a demo polls no feeds")
    elif settings.scheduler_enabled:
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
        version=__version__,
        docs_url="/api/docs" if settings.environment == "development" else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings

    # The only guard on a dashboard with no login: a POST that a browser says
    # came from somewhere else is refused. `web/security.py` says why that is the
    # proportionate answer here rather than a token in every form.
    app.middleware("http")(same_origin_only)

    # One environment for the life of the process, not one per render. Templates
    # are recompiled on change only in development, which is what `ENVIRONMENT`
    # has claimed in the env template since M0 and did not do.
    app.state.templates = build_templates(
        TEMPLATE_DIR, auto_reload=settings.environment == "development"
    )

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    register_routes(app)
    return app


app = create_app()
