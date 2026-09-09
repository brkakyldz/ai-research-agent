"""The reader's calendar, in one place.

Every stored timestamp is UTC (`db.models.UTCDateTime`) and every date the
application *means* is local: a bulletin's `day`, the run log's buckets, the
window a day's summaries fall in. Converting between the two is one rule, and it
was on the way to being four - `web.format.to_local`, `queries.recent_activity`,
the runner and a migration each needed it, and only the first two were in a
module the others could reach without importing the web layer.

Not `date(started_at)` in SQL, anywhere. Telling SQLite the offset writes a
second rule that disagrees with this one the first time the timezone setting
changes, and the disagreement is silent: a press at 01:30 in Antalya lands on
the previous day in the column and on today in the interface.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from ainews.config import get_settings


def local_zone() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().timezone)
    except Exception:
        # A bad IANA name must not take the dashboard down or stop a run; UTC
        # is legible and every timestamp is already in it.
        return ZoneInfo("UTC")


def to_local(value: datetime | None) -> datetime | None:
    """Every stored timestamp arrives aware and in UTC, so this converts and
    does not have to guess."""
    if value is None:
        return None
    return value.astimezone(local_zone())


def local_day(at: datetime | None = None) -> str:
    """The calendar day an instant belongs to, `YYYY-MM-DD`, as the reader counts."""
    return (to_local(at or datetime.now(UTC)) or datetime.now(UTC)).date().isoformat()


def day_bounds(day: str) -> tuple[datetime, datetime]:
    """The UTC half-open interval a local `YYYY-MM-DD` covers.

    What a query about "that day" needs, given that the column is UTC and the
    date is not.
    """
    zone = local_zone()
    start = datetime.fromisoformat(day).replace(tzinfo=zone)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)
