"""Formatting, and nothing that touches the database.

A date said the way each language says it, a duration at the scale it was
measured at, a score as one of three words, the Jinja environment the filters
are registered on. Every function here is a pure function of its arguments.

That is the whole point of it being its own module. `queries.py` needs three of
them — an age string on a story, a local date to bucket a run by — and `views.py`
needs `queries` for the shell's counts, so the two imported each other inside
function bodies four times over rather than admit the cycle at the top of the
file. There was no cycle to admit: the half `queries` wanted knows nothing about
runs or sessions, and the half that does know was never wanted back.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from ainews.config import Language, get_settings
from ainews.web.i18n import strings


def build_templates(directory: Path, *, auto_reload: bool) -> Jinja2Templates:
    """The Jinja environment, built once by `create_app`.

    It used to be built by `get_templates()` on every call, and there are twelve
    of those - so every page render parsed the loader's configuration, made a new
    environment, registered seven filters, and threw away the template cache the
    previous request had just filled. Jinja's whole compile-once design was being
    defeated by the accessor in front of it.

    `auto_reload` is the other half. `ENVIRONMENT=development` promised template
    reloading in the env template and only ever toggled `/api/docs`; a rebuilt
    environment made it true by accident and an environment built once would have
    made it false by accident. It is a parameter now, so the promise is kept on
    purpose and a production run gets the cache it is paying for.
    """
    templates = Jinja2Templates(directory=str(directory))
    # Starlette's wrapper forwards no environment options, so this is set after
    # the fact rather than passed in.
    templates.env.auto_reload = auto_reload
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
