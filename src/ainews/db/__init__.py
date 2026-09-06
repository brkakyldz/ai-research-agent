"""Persistence layer: models, engine/session wiring and schema creation."""

from ainews.db.models import Article, Base, DailyCounter, Run, Source, Summary, Verdict
from ainews.db.schema import init_db
from ainews.db.session import (
    create_engine,
    db_session,
    dispose_engine,
    get_engine,
    get_sessionmaker,
    session_scope,
)

__all__ = [
    "Article",
    "Base",
    "DailyCounter",
    "Run",
    "Source",
    "Summary",
    "Verdict",
    "create_engine",
    "db_session",
    "dispose_engine",
    "get_engine",
    "get_sessionmaker",
    "init_db",
    "session_scope",
]
