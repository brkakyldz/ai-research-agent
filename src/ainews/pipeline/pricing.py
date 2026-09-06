"""The price table, and the menu it doubles as.

Split out of `llm.py` on 2026-09-07 for one reason: importing that module costs
1.3 seconds of `langchain_openai`, and since ADR 0020 the *names* of the models
are needed in places that never build a client - `ainews sources` paying a
second and a third of startup to render `--model-summarize`'s choices would be a
tax on every command for the benefit of one.

So this module imports nothing but the standard library. The prices are per
million tokens and come from the model page read on 2026-09-04 (ADR 0001); they
are checked into the repo on purpose - a cost the dashboard shows has to be
reproducible, and a figure fetched at runtime is not.

`PRICES` is the single source for both jobs. Adding a tier is one line here: it
appears in the confirmation on `/runs`, in the CLI's `--model` choices and in
the cost arithmetic at once, and nothing else in the codebase may list a model
name.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# USD per 1M tokens: (input, output).
PRICES: dict[str, tuple[float, float]] = {
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-sol": (4.00, 20.00),
    "gpt-6-astra": (10.00, 50.00),
}
# An unknown model is priced as the most expensive one we know: a surprise in the
# billing dashboard is worse than a pessimistic number in ours.
FALLBACK_PRICE = (10.00, 50.00)

# Ordered names, for an `argparse` choices list. Cheapest first for the same
# reason the buttons are: the list a reader skims should start where the money
# does.
MODEL_NAMES: tuple[str, ...] = tuple(
    name for name, _ in sorted(PRICES.items(), key=lambda item: item[1])
)


def price_for(model: str) -> tuple[float, float]:
    for name, price in PRICES.items():
        if model.startswith(name):
            return price
    log.warning("no price known for model %r; costing it as the top tier", model)
    return FALLBACK_PRICE


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    price_in, price_out = price_for(model)
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """One offerable model: what to send, what to draw, what it costs."""

    id: str
    price_in: float
    price_out: float

    @property
    def short(self) -> str:
        """The last segment of the id - `gpt-5.6-luna` is `luna` on a button.

        Derived rather than stored: a second name for a model is a second place
        to keep it in step, and the ids already differ in exactly this segment.
        """
        return self.id.rsplit("-", 1)[-1]


def model_options(*configured: str) -> list[ModelChoice]:
    """The menu, cheapest first, with any configured model folded in.

    Cheapest first because the default is the cheapest and a control reads left
    to right: the tiers that cost ten and fifty times as much should be a reach,
    not a neighbour. A model set in the environment but absent from `PRICES` is
    appended rather than dropped - a picker that cannot draw the value it is
    currently set to would mark nothing and look broken.
    """
    options = [ModelChoice(name, *PRICES[name]) for name in MODEL_NAMES]
    known = {choice.id for choice in options}
    for name in configured:
        if name and name not in known:
            known.add(name)
            options.append(ModelChoice(name, *price_for(name)))
    return options


def resolve_model(value: str | None, fallback: str) -> str:
    """A model name from outside the process, or the configured default.

    The query string and `argv` are the two ways a model reaches a paid call, so
    an unrecognised name is answered with the default rather than an error: this
    is the same shape as `_valid` for the language, and a typo in a URL should
    cost the cheap run it was going to cost anyway, not a stack trace or - worse
    - a call priced at `FALLBACK_PRICE` against a model the vendor rejects.

    The fallback itself is always accepted, because the environment is allowed
    to name a model this table has not heard of yet.
    """
    return value if value in PRICES or value == fallback else fallback
