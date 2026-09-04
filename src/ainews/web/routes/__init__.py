"""Route registration.

One `register_routes(app)` so the application factory does not grow an import
list, and so a test can build an app with a subset of routers.
"""

from __future__ import annotations

from fastapi import FastAPI

from ainews.web.routes.digest import router as digest_router
from ainews.web.routes.health import router as health_router
from ainews.web.routes.runs import router as runs_router
from ainews.web.routes.sources import router as sources_router


def register_routes(app: FastAPI) -> None:
    app.include_router(digest_router)
    app.include_router(runs_router)
    app.include_router(sources_router)
    app.include_router(health_router)
