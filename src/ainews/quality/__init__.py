"""How good the output was, computed from rows for nothing.

Two layers measure this application and they are not the same layer.

`ainews/quality/` is **product code**. Nothing in it calls a model, spends a
cent or writes a row: `stories.py` reads one published day out of SQLite and
`checks.py` turns it into numbers. That is why the pipeline may call it - the
press stores its own checks on the bulletin it just published, so a person who
never opens a terminal still sees what the day scored.

`ainews/evals/` is the layer above it: the sampled grounding judge, the rank
probe, the fixture recorder and the dated record. Those cost money or write to
`eval_results`, and ADR 0019 §2 still holds for them exactly - nothing in the
product imports that package, and deleting it would break one CLI subcommand
and nothing else. The direction of the arrow is the whole rule: `evals/` reads
`quality/`, never the other way.

The split was made when the checks started running at publish (PLAN-V2 5.5).
Leaving them under `evals/` and letting `persist` import that package would have
made the free measurement and the paid one one thing, and the boundary that
matters - the product never spends money measuring itself - would have had no
line to stand on.
"""

from __future__ import annotations

from ainews.quality.checks import (
    content_floor,
    editor_note_shape,
    fallback_order,
    importance_distribution,
    numeral_values,
    ranked_order,
    ranker_vs_fallback,
    tag_vocabulary,
    tier_shape,
    ungrounded_numerals,
    unrepresented_fives,
    word_budget,
)
from ainews.quality.stories import day_stories, published_checks

__all__ = [
    "content_floor",
    "day_stories",
    "editor_note_shape",
    "fallback_order",
    "importance_distribution",
    "numeral_values",
    "published_checks",
    "ranked_order",
    "ranker_vs_fallback",
    "tag_vocabulary",
    "tier_shape",
    "ungrounded_numerals",
    "unrepresented_fives",
    "word_budget",
]
