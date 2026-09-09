"""How much three readings of the same day agree, and what to publish from them.

The ranker reads a numbered table and answers with numbers, so its answer can
depend on the order it was shown. That was measured occasionally by
`ainews eval rank-stability`, on a run already published, against an order the
reader had already been given. Here it is the production path: every bulletin is
three shuffled rank calls, aggregated, and the aggregate ships with the number
saying how much the three agreed (`bulletins.agreement`).

The measures live in the pipeline rather than in `evals/` because the pipeline
is now the thing that computes them, and `evals/` may import the pipeline while
nothing imports `evals/` (ADR 0019 §2). One implementation, one definition of
what a tau of 0.6 means.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from itertools import combinations

# One reading is not a measurement and two cannot be outvoted. Three is the
# smallest number that gives both an agreement figure and a majority.
RANK_PASSES = 3


def kendall_tau(a: Sequence[int], b: Sequence[int]) -> float:
    """Kendall's tau over the items both orders contain.

    1.0 for identical orders, -1.0 for reversed. Fewer than two shared items is
    **0.0 and not 1.0**: two orders that overlap in one story have not been
    shown to agree about anything, and scoring that as perfect agreement is how
    a gate on tau passes hardest exactly where the two readings share least.
    Total disagreement about membership reads as no agreement, which is what it
    is; the set measure beside it says how bad the overlap was.
    """
    in_b = set(b)
    shared = [x for x in a if x in in_b]
    if len(shared) < 2:
        return 0.0
    pos_b = {x: i for i, x in enumerate(b)}
    concordant = discordant = 0
    for x, y in combinations(shared, 2):
        # x precedes y in a; do they keep that order in b?
        if pos_b[x] < pos_b[y]:
            concordant += 1
        else:
            discordant += 1
    return (concordant - discordant) / (concordant + discordant)


def jaccard(a: Sequence[int], b: Sequence[int]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def expected_jaccard(kept: int, pool: int) -> float:
    """What two *random* selections of `kept` stories from `pool` would score.

    Two independent uniform subsets of size k from a pool of N share k**2/N
    items on average and cover 2k - k**2/N between them, so their Jaccard is
    k / (2N - k). Fifteen of twenty-seven scores 0.385 by coin flip, which is
    most of the way to a gate set at 0.6 - a ranker that had learned nothing
    would clear two thirds of the bar on the pool size alone.
    """
    if pool <= 0 or kept <= 0:
        return 0.0
    kept = min(kept, pool)
    return kept / (2 * pool - kept)


def chance_corrected_jaccard(observed: float, kept: int, pool: int) -> float:
    """`observed` rescaled so chance is 0.0 and identical selections are 1.0.

    Divided by the room above chance and not merely subtracted from it: with
    fifteen of twenty-seven the best achievable raw excess is 1 - 0.385 = 0.615,
    so a ranker that picked the *same fifteen every time* would score 0.615 and
    a gate at 0.6 would be measuring the pool size rather than the ranker.
    """
    expected = expected_jaccard(kept, pool)
    if expected >= 1.0:
        return 1.0
    return (observed - expected) / (1.0 - expected)


def mean_pairwise(
    orders: Sequence[Sequence[int]], measure: Callable[[Sequence[int], Sequence[int]], float]
) -> float:
    pairs = list(combinations(orders, 2))
    if not pairs:
        return 1.0
    return sum(measure(a, b) for a, b in pairs) / len(pairs)


def agreement(orders: Sequence[Sequence[int]], *, pool: int) -> float:
    """One number for "did the three readings describe the same day".

    The **lower** of the order measure and the set measure, because they fail
    independently and either failure is disqualifying: three orders can agree
    perfectly on five stories while disagreeing about which five belong, and
    they can pick the same fifteen stories in three unrelated orders. A mean of
    the two would let each hide the other.

    `pool` is how many candidates the readings chose from. The set measure is
    corrected against it, so both halves of the minimum are on the same scale:
    zero is what indifference scores and one is what agreement scores.
    """
    if len(orders) < 2:
        return 1.0
    kept = round(sum(len(order) for order in orders) / len(orders))
    return min(
        mean_pairwise(orders, kendall_tau),
        chance_corrected_jaccard(mean_pairwise(orders, jaccard), kept, pool),
    )


def borda(orders: Sequence[Sequence[int]], *, quorum: int = 2) -> list[int]:
    """Aggregate several rank orders into one.

    Borda and not "take the first call's answer": the first call is one sample
    of a stochastic process, and the reason for making three is that no single
    one of them is the day. A story scores `len(order) - position` in each list
    it appears in, so being second of twelve outscores being first of three -
    the count of stories a reading kept is itself part of what it said.

    `quorum` is a floor on *membership*, not on score. A story one reading of
    three put in the bulletin and the other two left out is a story the ranker
    does not agree is news; publishing it because it scored well in its one
    appearance is the failure this exists to prevent. With three passes the
    floor is two.

    Ties break on the best position the story reached anywhere, then on its id,
    so the result is a function of the inputs and not of dictionary order.
    """
    score: dict[int, float] = {}
    appearances: dict[int, int] = {}
    best: dict[int, int] = {}
    for order in orders:
        size = len(order)
        for position, item in enumerate(order):
            score[item] = score.get(item, 0.0) + (size - position)
            appearances[item] = appearances.get(item, 0) + 1
            best[item] = min(best.get(item, position), position)

    floor = min(quorum, len(orders))
    kept = [item for item, seen in appearances.items() if seen >= floor]
    return sorted(kept, key=lambda item: (-score[item], best[item], item))
