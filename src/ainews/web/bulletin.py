"""What a bulletin looks like on the page.

Editorial policy, not SQL. These were docstring essays inside `queries.py`,
which is a module named for the thing it is not: a query answers "which rows",
and these answer "which rows *should the reader see*, and how large". The
difference shows the moment you want to test one, because a policy question can
be asked of two integers and a query needs a database.

Most of what used to be here has gone rather than moved. `is_supplement`,
`supplement_floor` and `supplement_horizon` existed because the front page
showed a *run*, so a second press of the day produced a two-story bulletin that
would otherwise have replaced the morning's fifteen - three rules to hide one
modelling fault. A press now writes a new version of the day (ADR 0030), so
there is no supplement to detect, no floor to clear and no horizon to reach
back over: the front page is the newest version of the newest day, full stop.

`reading_order` has gone the same way. It existed to reconcile two scores -
`coalesce(editor_importance, importance)` for the size, `rank` for the tie -
and a bulletin reads in `position` order now, which is a column.
"""

from __future__ import annotations

from ainews.pipeline.state import TIER_STEP


def tier_step(tier: str | None) -> int | None:
    """The size step a tier draws at, or `None` for a story with no tier."""
    return TIER_STEP.get(tier) if tier else None
