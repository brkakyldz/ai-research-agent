"""Model construction and cost accounting.

One factory so every node gets the same timeout and retry policy, and one price
table so the run row can say what it cost. The prices are per million tokens and
come from the model page read on 2026-09-04 (ADR 0001); they are checked into the
repo on purpose - a cost the dashboard shows has to be reproducible, and a
figure fetched at runtime is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from langchain_openai import ChatOpenAI

from ainews.config import Settings, get_settings

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


@dataclass(slots=True)
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(self.tokens_in + other.tokens_in, self.tokens_out + other.tokens_out)


def price_for(model: str) -> tuple[float, float]:
    for name, price in PRICES.items():
        if model.startswith(name):
            return price
    log.warning("no price known for model %r; costing it as the top tier", model)
    return FALLBACK_PRICE


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    price_in, price_out = price_for(model)
    return (tokens_in * price_in + tokens_out * price_out) / 1_000_000


def usage_from_message(message: Any) -> Usage:
    """Read token counts off a LangChain message.

    `with_structured_output(..., include_raw=True)` is used everywhere precisely
    so this is possible: the parsed model comes back next to the raw response
    that still carries `usage_metadata`. Without the raw message there is no
    honest way to cost a run.
    """
    metadata = getattr(message, "usage_metadata", None) or {}
    if metadata:
        return Usage(int(metadata.get("input_tokens", 0)), int(metadata.get("output_tokens", 0)))
    response_metadata = getattr(message, "response_metadata", None) or {}
    legacy = response_metadata.get("token_usage") or {}
    return Usage(int(legacy.get("prompt_tokens", 0)), int(legacy.get("completion_tokens", 0)))


def make_llm(model: str, settings: Settings | None = None, **kwargs: Any) -> ChatOpenAI:
    settings = settings or get_settings()
    if not settings.llm_configured:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Copy the environment template to .env and fill it in."
        )
    return ChatOpenAI(
        model=model,
        api_key=settings.openai_api_key,
        timeout=settings.openai_timeout_seconds,
        # The fan-out is the reason this is not the default 2: a hundred parallel
        # branches will collect 429s, and backing off is cheaper than failing.
        max_retries=settings.openai_max_retries,
        **kwargs,
    )


def summarizer(settings: Settings | None = None) -> ChatOpenAI:
    settings = settings or get_settings()
    return make_llm(settings.openai_model_summarize, settings, temperature=0.2)


def ranker(settings: Settings | None = None) -> ChatOpenAI:
    settings = settings or get_settings()
    return make_llm(settings.openai_model, settings, temperature=0.1)


def judge(settings: Settings | None = None) -> ChatOpenAI:
    """The grounding judge (`ainews eval judge`): a stronger tier of the same
    vendor, at temperature 0 so a re-run judges the same way. Never called by
    the pipeline - only the evals layer constructs it (ADR 0019)."""
    settings = settings or get_settings()
    return make_llm(settings.openai_model_judge, settings, temperature=0)
