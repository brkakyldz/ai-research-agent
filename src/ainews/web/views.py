"""Everything a template needs that is not a database row.

Template rendering, the per-request language, the header strip's numbers, and the
formatting helpers. Routes stay thin because none of this belongs in them, and it
lives in one module because all four pages need the same header.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Language, get_settings
from ainews.db import Run, Source
from ainews.web.i18n import LANGUAGE_COOKIE, other_language, resolve_language, strings

TEMPLATE_DIR = None  # set by app.py at import time to avoid a circular import


def get_templates() -> Jinja2Templates:
    from ainews.web.app import TEMPLATE_DIR as directory

    templates = Jinja2Templates(directory=str(directory))
    templates.env.filters["clock"] = format_clock
    templates.env.filters["stamp"] = format_stamp
    templates.env.filters["money"] = format_money
    templates.env.filters["duration"] = format_duration
    return templates


def local_zone() -> ZoneInfo:
    settings = get_settings()
    try:
        return ZoneInfo(settings.timezone)
    except Exception:
        # A bad IANA name must not take the dashboard down; UTC is legible.
        return ZoneInfo("UTC")


def to_local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(local_zone())


def format_clock(value: datetime | None) -> str:
    local = to_local(value)
    return local.strftime("%d.%m %H:%M") if local else "—"


def format_stamp(value: datetime | None) -> str:
    local = to_local(value)
    return local.strftime("%d %b · %H:%M").lower() if local else "—"


def format_money(value: float | None) -> str:
    return f"${value or 0:.3f}"


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}:{rest:02d}"


def relative_age(value: datetime | None, language: Language) -> str:
    """How long ago, in the coarse units a news list actually needs."""
    local = to_local(value)
    if local is None:
        return ""
    delta = datetime.now(UTC) - local.astimezone(UTC)
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return "şimdi" if language == "tr" else "just now"
    if hours < 24:
        return f"{hours} saat" if language == "tr" else f"{hours}h"
    days = hours // 24
    return f"{days} gün" if language == "tr" else f"{days}d"


@dataclass(slots=True)
class Header:
    """The one-line status strip. Not a masthead - a status."""

    stamp: str
    stats: list[str]


async def build_header(session: AsyncSession, language: Language, run: Run | None = None) -> Header:
    t = strings(language)
    n_sources = (
        await session.execute(
            select(func.count()).select_from(Source).where(Source.enabled.is_(True))
        )
    ).scalar_one()

    if run is None:
        run = (
            await session.execute(
                select(Run)
                .where(Run.kind != "collect")
                .where(Run.status != "running")
                .order_by(Run.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    stats = [t["sources_count"].format(n=n_sources)]
    if run is not None:
        stats = [
            f"{min(run.n_summarized, get_settings().digest_top_n)} / {run.n_summarized}",
            t["sources_count"].format(n=n_sources),
            format_money(run.est_cost_usd),
            format_duration(run.duration_seconds),
        ]
    return Header(stamp=format_stamp(run.started_at if run else datetime.now(UTC)), stats=stats)


def base_context(request: Request, language: Language, page: str) -> dict[str, object]:
    """The keys `base.html` needs on every page."""
    other = other_language(language)
    query = dict(request.query_params)
    query["lang"] = other
    toggle = request.url.replace_query_params(**query)
    return {
        "request": request,
        "language": language,
        "t": strings(language),
        "page": page,
        "toggle_url": str(toggle.path) + ("?" + toggle.query if toggle.query else ""),
    }


def language_of(request: Request) -> Language:
    return resolve_language(request.query_params.get("lang"), request.cookies.get(LANGUAGE_COOKIE))


def remember_language(response, language: Language) -> None:
    """Persist the choice so tomorrow's first visit is already right."""
    response.set_cookie(
        LANGUAGE_COOKIE,
        language,
        max_age=60 * 60 * 24 * 365,
        samesite="lax",
        httponly=False,
    )
