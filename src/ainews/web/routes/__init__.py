"""Route registration.

One `register_routes(app)` so the application factory does not grow an import
list, and so tests can build an app with a subset of routers.
"""

from __future__ import annotations

from fastapi import FastAPI

from ainews.web.routes.health import router as health_router


def register_routes(app: FastAPI) -> None:
    app.include_router(health_router)
