"""The strict xfails, counted and named in one place.

There are fourteen, and finding 16 of the 2026-09-08 audit called them "permanent
fixtures instead of tracked issues". They are not permanent — `strict=True` is
what makes them temporary, because the day a gap closes the mark fails loudly
rather than passing silently — but the audit's real complaint stands: nothing
outside the two files that declare them said how many there were or what they
were waiting on. A gap nobody counts is a gap nobody closes.

This is the count. It fails when a gap is added without a reason, and it fails
when the total changes, so adding or closing one is a deliberate edit here with
the number in the diff.

The two sources:

- `test_dedupe.py`, recall — nine golden pairs from the 2026-09-04 run that
  `token_set_ratio` cannot reach: the same event written up by different outlets
  in different words. They *are* the E5 trigger ("golden pairs keep failing on
  new outlets → embedding dedupe"), so the mark is the measurement.
- `test_dedupe.py`, precision — two pairs that share every number and carry no
  negation, so `contradicts` has nothing lexical to refuse them on: one product
  name apart, and one subject apart. Counted separately because they wait on the
  same embedding for the opposite reason - the recall gaps are merges that do
  not happen, these are merges that should not.
- `test_evals_checks.py::KNOWN_GAPS` — three checks the 2026-09-04 fixture
  cannot meet because it was recorded before the fix each one exists for. They
  come off when a newer run is recorded, which is a fact about that file rather
  than about the code.
"""

from __future__ import annotations

# The number of `xfail(strict=True)` outcomes the suite is expected to produce,
# and where each group comes from. Change this line and say why in the commit.
EXPECTED = {
    "golden pairs that should merge and cannot (E5 trigger)": 9,
    "golden pairs that should not merge and do (E5 trigger)": 2,
    "checks the 2026-09-04 fixture predates": 3,
}


def test_the_known_gaps_are_the_number_we_think_they_are() -> None:
    """Counted off the declarations rather than off a test run, so this does not
    depend on being run with the whole suite."""
    from ainews.evals.record import FIXTURE_DIR
    from test_dedupe import GOLDEN_PAIRS
    from test_evals_checks import KNOWN_GAPS

    marked = [row for row in GOLDEN_PAIRS if getattr(row, "marks", ())]
    # The third value of the row is `should_merge`, which is what separates a
    # missed duplicate from a wrong one.
    missed = sum(1 for row in marked if row.values[2])
    wrong = sum(1 for row in marked if not row.values[2])
    fixtures = {p.name for p in FIXTURE_DIR.glob("*.json")}
    # A gap declared against a fixture that is no longer recorded is not a gap.
    predated = sum(len(gaps) for name, gaps in KNOWN_GAPS.items() if name in fixtures)

    assert missed == EXPECTED["golden pairs that should merge and cannot (E5 trigger)"]
    assert wrong == EXPECTED["golden pairs that should not merge and do (E5 trigger)"]
    assert predated == EXPECTED["checks the 2026-09-04 fixture predates"]
    assert missed + wrong + predated == sum(EXPECTED.values()) == 14


def test_every_known_gap_states_what_it_is_waiting_on() -> None:
    """A bare `xfail` is a test switched off. A reason is the difference between
    a measurement and a silence."""
    from test_evals_checks import KNOWN_GAPS

    for name, gaps in KNOWN_GAPS.items():
        for check, reason in gaps.items():
            assert reason.strip(), f"{name}/{check} is marked with no reason"
            assert len(reason) > 20, f"{name}/{check}: {reason!r} does not say what it waits on"
