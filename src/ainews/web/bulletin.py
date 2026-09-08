"""What counts as the day's bulletin, and in what order it reads.

Editorial policy, not SQL. Both rules here were written as docstring essays
inside `queries.py`, which is a module named for the thing it is not: a query
answers "which rows", and these answer "which rows *should the reader see*".
The difference shows the moment you want to test one, because a policy question
can be asked of two integers and a query needs a database.

So: pure functions of a run and the settings, with unit tests of their own, and
`queries.py` composes them into statements.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, func

from ainews.config import Settings
from ainews.db import Run, Summary


def supplement_floor(settings: Settings) -> int:
    """How many summaries a run needs before it can be the day's bulletin.

    Half of `digest_top_n`, and at least one. Half rather than a number of its
    own because the cap is already the statement of how big a bulletin is, and a
    second setting would be the same decision made twice — the pair would drift
    the first time anyone raised the cap.
    """
    return max(1, settings.digest_top_n // 2)


def is_supplement(run: Run, settings: Settings) -> bool:
    """A paid run too small to be the front page on its own.

    Every press is a delta: only what has not been summarised is a candidate. So
    a second press ten minutes after a fifteen-story bulletin summarises the two
    articles that arrived in between, ranks them, and writes a three-paragraph
    note about a day of two stories. Until 2026-09-08 that two-story run became
    the front page and the morning's bulletin dropped into the archive.

    It is still a real run and it stays in the archive. What it is not is the
    day.
    """
    return run.n_summarized < supplement_floor(settings)


def supplement_horizon(run: Run, settings: Settings) -> datetime:
    """How far back a supplement may reach for the bulletin it supplements.

    `digest_suggest_after_hours`, which is the interval `/runs` already counts a
    day by — one definition of "the same day's news", not a second one invented
    here. Past that horizon the small run *is* the day's bulletin and is shown,
    because a stale full page would be worse than a thin fresh one.
    """
    return run.started_at - timedelta(hours=settings.digest_suggest_after_hours)


def reading_order() -> tuple[ColumnElement[object], ...]:
    """The order a bulletin reads in: heaviest first.

    Importance leads and `rank` only breaks its ties. It was the other way round
    until 2026-09-08, and the docstring claimed exactly what this one claims —
    that the reader scanning downward sees the ink fade monotonically — while the
    query made it false. `rank` is the ranker's order over the run, `importance`
    is a 1-5 score on one story, and the two disagree constantly. A real day came
    out p4, p3, p3, p2, p3, which on a page whose only ranking indicator is the
    size of the headline (ADR 0014) reads as no order at all.

    *Whose* importance is the other half (ADR 0025, the same day). The ranker is
    the one node that sees the whole day and is told to correct the summariser's
    isolated scores; `editor_importance` is where those corrections land, and
    this reads it where it exists. The summariser's score still orders the
    stories below the fold and every run from before the column.

    `rank` still decides *which* stories are in the bulletin — that is the top-N
    filter, and the job it was written for. What it no longer does is decide the
    order they are read in, which is what the typography is already saying.
    """
    return (
        func.coalesce(Summary.editor_importance, Summary.importance).desc(),
        Summary.rank.asc().nullslast(),
        Summary.id.asc(),
    )
