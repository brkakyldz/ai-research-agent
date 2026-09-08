"""Everything a template needs that is not a database row.

Template rendering, the per-request language and theme, the shell's numbers, and
the formatting helpers. Routes stay thin because none of this belongs in them,
and it lives in one module because all five pages wear the same shell.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Language, Settings, get_settings
from ainews.db import Run, Source, Summary, bulletin_runs
from ainews.web.i18n import (
    LANGUAGE_COOKIE,
    LANGUAGES,
    THEME_COOKIE,
    THEMES,
    resolve_language,
    resolve_theme,
    strings,
)


def get_templates() -> Jinja2Templates:
    from ainews.web.app import TEMPLATE_DIR as directory

    templates = Jinja2Templates(directory=str(directory))
    templates.env.filters["clock"] = format_clock
    templates.env.filters["stamp"] = stamp_filter
    templates.env.filters["money"] = format_money
    templates.env.filters["duration"] = format_duration
    templates.env.filters["number"] = number_filter
    templates.env.filters["band"] = impact_band
    templates.env.filters["paragraphs"] = split_paragraphs
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


# Month abbreviations, per language. `%b` gives the C locale's English in both
# languages, and until 2026-09-06 it was lower-cased on top - so a Turkish page
# read "04 sep" wherever the stamp was drawn without the `.mono` class that
# uppercases it, which is the exact failure ADR 0016 moved the capitals into
# the string table to prevent.
MONTHS: dict[Language, tuple[str, ...]] = {
    "tr": ("Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"),
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
}


def format_stamp(value: datetime | None, language: Language = "en") -> str:
    """Day, month and time, in the reader's language: "04 Eyl · 14:18"."""
    local = to_local(value)
    if local is None:
        return "—"
    month = MONTHS.get(language, MONTHS["en"])[local.month - 1]
    return f"{local:%d} {month} · {local:%H:%M}"


# The months again, written out. The bar draws the bulletin's date the way a
# person would say it - "6 Eylül 2026" - and every other stamp in the app stays
# short, because every other stamp is one row of a table or one line of a log
# where the abbreviation is what keeps the column narrow.
MONTHS_LONG: dict[Language, tuple[str, ...]] = {
    "tr": (
        "Ocak",
        "Şubat",
        "Mart",
        "Nisan",
        "Mayıs",
        "Haziran",
        "Temmuz",
        "Ağustos",
        "Eylül",
        "Ekim",
        "Kasım",
        "Aralık",
    ),
    "en": (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ),
}


def format_stamp_long(value: datetime | None, language: Language = "en") -> str:
    """The bulletin's date, spelled out: "6 Eylül 2026 · 16:06".

    No leading zero on the day, because nothing is being lined up in a column
    here - this is the one date in the interface that is read as a sentence
    rather than scanned down a table. Each language puts the parts in its own
    order, which is the whole reason this is not a format string with a
    placeholder in it.
    """
    local = to_local(value)
    if local is None:
        return "—"
    month = MONTHS_LONG.get(language, MONTHS_LONG["en"])[local.month - 1]
    if language == "en":
        return f"{month} {local.day}, {local.year} · {local:%H:%M}"
    return f"{local.day} {month} {local.year} · {local:%H:%M}"


@pass_context
def stamp_filter(context, value: datetime | None) -> str:
    """`|stamp` in a template, reading the page's language off the render context."""
    return format_stamp(value, context.get("language", "en"))


@pass_context
def number_filter(context, value: int | None) -> str:
    """`|number` in a template: a count grouped in the page's own notation.

    The separator is a string in `i18n` for the same reason `u_sep` is - notation
    is a language's habit, not a rule to be hard-coded in a formatter - but this
    one carries more than a space. "." and "," are each other's decimal point
    between these two languages, so a token count grouped the English way does
    not read as foreign on a Turkish page, it reads as a different number:
    "50,521" is fifty-and-a-half.
    """
    sep = strings(context.get("language", "en")).get("n_sep", ",")
    return f"{value or 0:,}".replace(",", sep)


def format_money(value: float | None) -> str:
    return f"${value or 0:.3f}"


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}:{rest:02d}"


def format_step_duration(t: dict[str, str], seconds: float | None) -> str:
    """One node's span, at a node's scale.

    `format_duration` is a run's clock and reads `0:41`. A step is often under a
    second, and four of the six nodes in a digest are: on that scale the run's
    format collapses `0.4s` and `0.3s` and `24.6s`'s neighbours all to `0:00`,
    which was visible the first time the page was drawn against a real run and
    invisible in the mockup, where the numbers were typed by hand.

    So: seconds with one decimal under a minute, the run's own shape above it.
    A step too fast to have a first decimal still gets one rather than a zero -
    `< 0.1s` is a measurement, `0.0s` reads as a step that did not happen.

    The units and the decimal point come from `t`, which is why this takes the
    strings dict and is not a Jinja filter: Turkish writes 24,6 and English
    24.6, and the same three characters printed in both languages would be a
    different number in one of them.
    """
    if seconds is None:
        return "—"
    if seconds < 0.05:
        return f"< 0{t['d_sep']}1{t['u_sep']}{t['u_sec']}"
    # Branch on what will be printed, not on what was measured. Testing the raw
    # value and then rounding it put 59.96s in the under-a-minute arm, where one
    # decimal rounds it to "60,0 sn" - sixty seconds written in the format this
    # function leaves at sixty, one tick before the same span reads "1 dk 0 sn".
    tenths = round(seconds, 1)
    if tenths < 60:
        return f"{tenths:.1f}".replace(".", t["d_sep"]) + t["u_sep"] + t["u_sec"]
    minutes, rest = divmod(int(tenths), 60)
    return f"{minutes}{t['u_sep']}{t['u_min']}{t['u_sep']}{rest}{t['u_sep']}{t['u_sec']}"


BLANK_LINE = re.compile(r"\n\s*\n")


def split_paragraphs(text: str | None) -> list[str]:
    """The brief, as the paragraphs the model actually wrote.

    The note is one string in the database and one string in the model's output,
    with blank lines between its parts - so the split belongs here rather than
    in three columns of the schema, which would fix the count at three forever.
    A note written before the prompt asked for three paragraphs has no blank
    line in it and comes back as a single paragraph, which is what it is.
    """
    if not text:
        return []
    blocks = re.split(BLANK_LINE, text.strip())
    return [" ".join(block.split()) for block in blocks if block.strip()]


def impact_band(importance: int) -> str:
    """The 1-5 score as one of three words.

    The bars alone were a shape nobody had been told how to read - five ticks,
    some filled, no legend anywhere on the page. Naming the band is what turns
    them into a scale; three names rather than five because the reader is
    sorting, not grading, and five labels would just be the number again.
    """
    if importance >= 4:
        return "high"
    if importance == 3:
        return "mid"
    return "low"


def impact_split(stories: list) -> list[tuple[str, int]]:
    """How many of the stories in view fall in each band, high first.

    The side column's one summary of the feed itself. It is counted off the list
    the reader is actually looking at rather than off the run, so it answers the
    filtered page too: with `?tag=openai` on, it says how heavy *that* topic's
    day was. A band with nothing in it still gets its row - a scale with holes
    in it is read as a scale, and one with rows appearing and disappearing is
    read as three unrelated numbers.
    """
    counts = dict.fromkeys(("high", "mid", "low"), 0)
    for story in stories:
        counts[impact_band(story.importance)] += 1
    return list(counts.items())


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


async def build_header(
    session: AsyncSession,
    language: Language,
    run: Run | None = None,
    n_sources: int | None = None,
    n_stories: int | None = None,
) -> Header:
    if n_sources is None:
        n_sources = await count_enabled_sources(session)

    if run is None:
        run = await latest_finished_run(session)

    if n_stories is None and run is not None:
        n_stories = await count_ranked(session, run)

    return Header(
        # No run, no stamp: the bar used to show the current time, which is a
        # number nobody measured dressed as one somebody did.
        stamp=format_stamp_long(run.started_at, language) if run else None,
        n_stories=n_stories,
        n_sources=n_sources,
        bulletin_language=(run.language if run is not None and run.language != language else None),
    )


async def count_ranked(session: AsyncSession, run: Run) -> int:
    """How many stories the digest actually holds.

    It was `min(run.n_summarized, digest_top_n)` until 2026-09-06 - a guess, and
    on a day the ranker returns fewer than the cap it is the wrong one. On
    2026-09-05 the run summarised 136 and ranked 11; the page drew eleven
    stories under a rail badge reading 15 and a footnote reading "15 haber".

    `digest_top_n` is a ceiling the ranker is asked to respect, not a promise it
    made. The number of stories on the page is a `COUNT`, so it is counted.
    """
    return (
        await session.execute(
            select(func.count())
            .select_from(Summary)
            .where(Summary.run_id == run.id)
            .where(Summary.rank.isnot(None))
        )
    ).scalar_one()


async def latest_finished_run(session: AsyncSession) -> Run | None:
    """The most recent run that produced something, in any language.

    A run still going has no numbers yet, and a collect has no digest in it -
    neither is what the status strip is reporting on.
    """
    return (
        await session.execute(bulletin_runs().where(Run.status != "running").limit(1))
    ).scalar_one_or_none()


async def count_enabled_sources(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(Source).where(Source.enabled.is_(True))
        )
    ).scalar_one()


def format_gap(seconds: float, language: Language) -> str:
    """A span of time in the units a person would say it in.

    Coarse on purpose, and coarser the further out it is: the reader is deciding
    whether to press a button now or after breakfast, so minutes matter under an
    hour and stop mattering after two days. The countdown script in `runs.html`
    reproduces these branches exactly - one rule, drawn twice, because the server
    has to render a first frame that JavaScript then keeps ticking. The words
    themselves are not drawn twice: both copies read them off `i18n`, which is
    also why "3h 25m" closes up in English and "3 sa 25 dk" does not in Turkish -
    that space is `u_sep`, a translated string like any other.
    """
    t = strings(language)
    sep = t["u_sep"]
    minutes = int(abs(seconds) // 60)
    if minutes < 1:
        return t["u_moment"]
    if minutes < 60:
        return f"{minutes}{sep}{t['u_min']}"
    hours, rest = divmod(minutes, 60)
    if hours >= 48:
        return f"{hours // 24}{sep}{t['u_day']}"
    if rest:
        return f"{hours}{sep}{t['u_hour']} {rest}{sep}{t['u_min']}"
    return f"{hours}{sep}{t['u_hour']}"


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
    from ainews.pipeline.runner import digest_in_flight
    from ainews.web import queries

    settings = settings or get_settings()
    history = await queries.run_history(session)
    interval = settings.digest_suggest_after_hours

    due_at = None
    seconds_left = None
    since = None
    if history.last_success_at is not None:
        anchor = history.last_success_at
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=UTC)
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
    from ainews.web import queries

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
    run: Run | None = None,
) -> dict[str, object]:
    """`base_context` plus the two things the shell reads from the database.

    One helper because the rail and the status strip need the same source count,
    and paying for it twice on every page would be the only cost of splitting.
    """
    n_sources = await count_enabled_sources(session)
    if run is None:
        run = await latest_finished_run(session)
    # One count, two readers. The rail badge and the brief's footnote are the
    # same number and used to be arrived at twice by the same wrong formula.
    n_stories = await count_ranked(session, run) if run is not None else None
    context = base_context(request, language, page)
    context["header"] = await build_header(
        session, language, run, n_sources=n_sources, n_stories=n_stories
    )
    # One advice object, two readers: the rail's foot on every page and the block
    # at the head of `/runs`. It is built here so the two can never disagree.
    context["advice"] = advice = await build_advice(session, language)
    context["rail"] = await build_rail(session, language, advice, n_sources, n_stories)
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
