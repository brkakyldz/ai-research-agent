"""The editorial rules, asked directly.

Both of these were docstring essays inside `queries.py` and could only be tested
through a rendered page. They are pure functions of a run and the settings now,
which means the boundary cases can be stated as arithmetic rather than staged as
five database rows and an HTTP request.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from ainews.config import Settings
from ainews.db import Run, Summary
from ainews.db.models import utcnow
from ainews.web.bulletin import (
    is_supplement,
    reading_order,
    supplement_floor,
    supplement_horizon,
)


def _run(n: int) -> Run:
    return Run(kind="digest", language="tr", status="ok", n_summarized=n, started_at=utcnow())


@pytest.mark.parametrize("top_n,floor", [(15, 7), (2, 1), (1, 1), (100, 50)])
def test_the_floor_is_half_the_cap_and_never_zero(
    settings: Settings, top_n: int, floor: int
) -> None:
    """Half of `digest_top_n` rather than a setting of its own: the cap already
    states how big a bulletin is, and a second knob would drift from it the first
    time anyone raised the first. `digest_top_n=1` is the case that would make a
    floor of zero and let a run of nothing be the day."""
    settings.digest_top_n = top_n
    assert supplement_floor(settings) == floor


def test_a_run_at_the_floor_is_the_day_and_one_under_it_is_not(settings: Settings) -> None:
    settings.digest_top_n = 15
    assert is_supplement(_run(6), settings) is True
    assert is_supplement(_run(7), settings) is False
    assert is_supplement(_run(15), settings) is False


def test_a_run_of_nothing_is_a_supplement(settings: Settings) -> None:
    """Two stories arriving ten minutes after a fifteen-story bulletin is the
    case this exists for: the press is a delta, so it summarises the two and
    writes a three-paragraph note about a day of two stories."""
    settings.digest_top_n = 15
    assert is_supplement(_run(2), settings) is True


def test_the_horizon_is_the_interval_runs_already_counts_a_day_by(settings: Settings) -> None:
    """One definition of "the same day's news", not a second one invented for
    the front page."""
    settings.digest_suggest_after_hours = 24
    run = _run(2)
    assert supplement_horizon(run, settings) == run.started_at - timedelta(hours=24)

    settings.digest_suggest_after_hours = 6
    assert supplement_horizon(run, settings) == run.started_at - timedelta(hours=6)


def test_importance_leads_the_reading_order_and_rank_breaks_its_ties() -> None:
    """Led by `rank` instead, on a page whose only ranking indicator is the size
    of the headline, a real day comes out p4, p3, p3, p2, p3 - which reads as no
    order at all."""
    clauses = reading_order()
    assert len(clauses) == 3
    first, second, third = (str(c) for c in clauses)
    assert "coalesce" in first.lower()
    assert "editor_importance" in first and "importance" in first
    assert first.endswith("DESC")
    assert second.startswith("summaries.rank")
    assert third.startswith("summaries.id")


def test_the_editors_score_wins_where_the_ranker_gave_one() -> None:
    """ADR 0025: the ranker is the one node that sees the whole day and is told
    to correct the summariser's isolated scores. Before the column existed they
    had nowhere to land, so the correction was thrown away."""
    first = str(reading_order()[0])
    assert first.index("editor_importance") < first.index("summaries.importance")
    assert Summary.editor_importance is not None
