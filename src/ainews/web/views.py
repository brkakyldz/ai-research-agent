"""The shell every page wears, and what it costs to draw.

The header, the rail and its counts, the advice block, the per-request language
and theme. Everything here either reads the database or reads the request;
routes stay thin because none of it belongs in them, and it lives in one module
because all five pages wear the same shell.

The formatting half moved to `web/format.py` on 2026-09-08. It had nothing to do
with either a session or a request, and keeping it here is what made `queries`
and `views` import each other inside four function bodies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Language, Settings, get_settings
from ainews.db import Bulletin, BulletinItem, Run, Source
from ainews.pipeline.api import digest_in_flight
from ainews.web import queries
from ainews.web.format import format_gap, format_stamp_long
from ainews.web.i18n import (
    LANGUAGE_COOKIE,
    LANGUAGES,
    THEME_COOKIE,
    THEMES,
    resolve_language,
    resolve_theme,
    strings,
)


def get_templates(request: Request) -> Jinja2Templates:
    """The one environment this app was built with."""
    return request.app.state.templates  # type: ignore[no-any-return]


@dataclass(slots=True)
class Header:
    """What the top bar reports. Not a masthead, and not a run report either.

    The cost and the duration of the last run used to sit here. They are
    execution metrics: they tell the reader how the page was made, not what is
    in it, and they were the two loudest things on a strip the reader passes
    through every morning. They live on the run log now, next to the rest of the
    row they belong to. What stays is what a reader would ask for: when this was
    made, and how much of it there is.
    """

    stamp: str | None
    n_stories: int | None
    n_sources: int
    # The language the bulletin was written in, and only when it is not the
    # language the page is drawn in. Since the shell's switch stopped filtering
    # content (ADR 0017), a Turkish reader can be looking at an English digest;
    # that is allowed, but it is not allowed to be unannounced. Same language,
    # no mark - a label that is always true tells the reader nothing.
    bulletin_language: str | None = None


@dataclass(slots=True)
class Rail:
    """What the left rail needs beyond the links themselves.

    The counts are the rail's whole reason to carry numbers: a nav item that
    says how much is behind it answers "is there anything new" without a click.
    Nothing here is a new page's worth of data - three counts and one line of
    advice, which is the foot's whole content since the clock stopped being the
    thing that starts a digest.
    """

    counts: dict[str, int | None]
    advice: Advice


def build_header(
    language: Language,
    bulletin: Bulletin | None,
    n_sources: int,
    n_stories: int | None,
) -> Header:
    """Pure: `shell_context` has already paid for every number in here.

    It used to take three of them as optional and fetch each one itself when it
    was not given, which read as a convenience and worked out as a second copy
    of the same lookup on every page render. Its only caller has the bulletin,
    the count and the source total in hand.
    """
    return Header(
        # No bulletin, no stamp: the bar used to show the current time, which is
        # a number nobody measured dressed as one somebody did.
        stamp=format_stamp_long(bulletin.created_at, language) if bulletin else None,
        n_stories=n_stories,
        n_sources=n_sources,
        bulletin_language=(
            bulletin.language if bulletin is not None and bulletin.language != language else None
        ),
    )


async def count_items(session: AsyncSession, bulletin: Bulletin) -> int:
    """How many stories the bulletin actually holds.

    A `COUNT`, not `min(n_summarized, digest_top_n)`. That arithmetic is a guess
    and on any day the ranker returns fewer than the cap it is the wrong one: a
    run that summarised 136 and ranked 11 drew eleven stories under a rail badge
    reading 15 and a footnote reading "15 haber". `digest_top_n` is a ceiling
    the ranker is asked to respect and now explicitly told it need not reach
    (`prompts/*/rank.md`), so the number on the page is counted.
    """
    return (
        await session.execute(
            select(func.count())
            .select_from(BulletinItem)
            .where(BulletinItem.bulletin_id == bulletin.id)
        )
    ).scalar_one()


async def count_enabled_sources(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(Source).where(Source.enabled.is_(True))
        )
    ).scalar_one()


@dataclass(slots=True)
class Advice:
    """Whether it is worth pressing the button, and why.

    The digest costs money, so nothing starts it but a person (ADR 0015). What
    replaces the clock is this: one state, the reason for it, and a countdown to
    the moment the tool would suggest a run. It advises and never refuses - the
    only two things that actually stop a press are a run already in flight and a
    missing key, and both of those are facts about the machine rather than
    opinions about the timing.
    """

    state: str  # running | blocked | never | due | waiting
    last: Run | None
    last_collect_at: datetime | None
    since: str | None  # how long since the last successful digest
    due_at: datetime | None
    seconds_left: float | None  # negative once the suggested time has passed
    gap: str | None  # `seconds_left`, said in words
    interval_hours: int

    @property
    def ready(self) -> bool:
        """A press would start a run right now. Not the same as advisable."""
        return self.state not in ("running", "blocked")

    @property
    def due_epoch_ms(self) -> int | None:
        """The countdown's target, in the one format JavaScript reads for free."""
        if self.due_at is None:
            return None
        return int(self.due_at.timestamp() * 1000)


async def build_advice(
    session: AsyncSession,
    language: Language,
    settings: Settings | None = None,
) -> Advice:
    settings = settings or get_settings()
    history = await queries.run_history(session)
    interval = settings.digest_suggest_after_hours

    due_at = None
    seconds_left = None
    since = None
    if history.last_success_at is not None:
        anchor = history.last_success_at
        due_at = anchor + timedelta(hours=interval)
        now = datetime.now(UTC)
        seconds_left = (due_at - now).total_seconds()
        since = format_gap((now - anchor).total_seconds(), language)

    if digest_in_flight():
        state = "running"
    elif not settings.llm_configured:
        state = "blocked"
    elif history.last_success_at is None:
        state = "never"
    elif seconds_left is not None and seconds_left > 0:
        state = "waiting"
    else:
        state = "due"

    return Advice(
        state=state,
        last=history.last_finished,
        last_collect_at=history.last_collect_at,
        since=since,
        due_at=due_at,
        seconds_left=seconds_left,
        gap=format_gap(seconds_left, language) if seconds_left is not None else None,
        interval_hours=interval,
    )


async def build_rail(
    session: AsyncSession,
    language: Language,
    advice: Advice,
    n_sources: int,
    n_stories: int | None = None,
) -> Rail:
    n_archive = await queries.count_archive(session)
    return Rail(
        counts={
            "digest": n_stories,
            "archive": n_archive or None,
            "sources": n_sources,
        },
        advice=advice,
    )


def base_context(request: Request, language: Language, page: str) -> dict[str, object]:
    """The keys `base.html` needs on every page."""
    return {
        "request": request,
        "language": language,
        "t": strings(language),
        "page": page,
        "theme": theme_of(request),
        "theme_urls": theme_urls(request),
        "language_urls": language_urls(request),
    }


async def shell_context(
    request: Request,
    session: AsyncSession,
    language: Language,
    page: str,
    bulletin: Bulletin | None = None,
) -> dict[str, object]:
    """`base_context` plus the things the shell reads from the database.

    One helper because the rail and the status strip need the same source count,
    and paying for it twice on every page would be the only cost of splitting.

    The advice is built first, which is not an ordering preference: the rail's
    foot on every page and the block at the head of `/runs` read the same
    object, so building it here is what stops the two disagreeing.

    A page that is not about one bulletin - `/sources`, `/search` - passes none
    and the shell falls back to the current one, because the bar's stamp and the
    rail's story count describe what the reader would see on the front page
    rather than what they are looking at.
    """
    n_sources = await count_enabled_sources(session)
    context = base_context(request, language, page)
    context["advice"] = await build_advice(session, language)
    if bulletin is None:
        bulletin = await queries.latest_bulletin(session)
    # One count, two readers. The rail badge and the brief's footnote are the
    # same number and used to be arrived at twice by the same wrong formula.
    n_stories = await count_items(session, bulletin) if bulletin is not None else None
    context["header"] = build_header(language, bulletin, n_sources, n_stories)
    context["rail"] = await build_rail(session, language, context["advice"], n_sources, n_stories)
    return context


def language_of(request: Request) -> Language:
    return resolve_language(request.query_params.get("lang"), request.cookies.get(LANGUAGE_COOKIE))


def theme_of(request: Request) -> str:
    return resolve_theme(request.query_params.get("theme"), request.cookies.get(THEME_COOKIE))


def language_urls(request: Request) -> dict[str, str]:
    """Where each language points, from wherever the reader is standing.

    This used to be a single `toggle_url`: one link, labelled with the language
    you would get if you pressed it. It is a two-slot switch now for the reason
    the theme control is one - a control that shows only the state you are not
    in makes the reader work out which of the two words is the current setting,
    and `english` on a Turkish page reads as plausibly either. Both slots drawn,
    the current one marked, and there is nothing left to work out.
    """
    return _with_param(request, "lang", LANGUAGES)


def theme_urls(request: Request) -> dict[str, str]:
    """Where each theme choice points, from wherever the reader is standing.

    The theme takes the same shape as the language: a `?theme=` that wins, a
    cookie it writes, and a default. That makes it one rule to hold instead of
    two, and it means the switch is three links - no JavaScript, and no flash
    of the wrong theme, because the server already knows before it renders.
    """
    return _with_param(request, "theme", THEMES)


def _with_param(request: Request, name: str, values: tuple[str, ...]) -> dict[str, str]:
    """The current URL, once per value of one query parameter."""
    urls = {}
    for value in values:
        query = dict(request.query_params)
        query[name] = value
        target = request.url.replace_query_params(**query)
        urls[value] = str(target.path) + ("?" + target.query if target.query else "")
    return urls


def remember_language(response, language: Language) -> None:
    """Persist the choice so tomorrow's first visit is already right."""
    _remember(response, LANGUAGE_COOKIE, language)


def remember_preferences(request: Request, response, language: Language) -> None:
    """Write back both choices at once, because every page makes both."""
    remember_language(response, language)
    _remember(response, THEME_COOKIE, theme_of(request))


def _remember(response, name: str, value: str) -> None:
    response.set_cookie(
        name,
        value,
        max_age=60 * 60 * 24 * 365,
        samesite="lax",
        httponly=False,
    )
