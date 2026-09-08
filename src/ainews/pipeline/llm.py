"""Model construction, and the token counts the price table is applied to.

One factory so every node gets the same timeout and retry policy. The prices
live in `pricing`, which that module says why, and are imported from there: they
were re-exported through here for a while so existing call sites would keep
working, which in a repository with one author and one commit history is a
compatibility layer against nobody. The call sites moved.

Since ADR 0020 each factory takes an optional model. Which one a run uses is the
caller's to decide, not this module's to look up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from langchain_openai import ChatOpenAI

from ainews.config import Settings, get_settings

__all__ = [
    "Usage",
    "judge",
    "make_llm",
    "ranker",
    "summarizer",
    "usage_from_message",
]

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(self.tokens_in + other.tokens_in, self.tokens_out + other.tokens_out)


def usage_from_message(message: Any) -> Usage:
    """Read token counts off a LangChain message.

    `with_structured_output(..., include_raw=True)` is used everywhere precisely
    so this is possible: the parsed model comes back next to the raw response
    that still carries `usage_metadata`. Without the raw message there is no
    honest way to cost a run.
    """
    metadata = getattr(message, "usage_metadata", None) or {}
    tokens_in = int(metadata.get("input_tokens", 0))
    tokens_out = int(metadata.get("output_tokens", 0))
    # Fall through on *unusable* counts, not on a missing dict. A present
    # `usage_metadata` whose keys have been renamed upstream reads as zero and
    # used to return here, skipping the legacy path that might still have
    # answered.
    if not tokens_in and not tokens_out:
        legacy = (getattr(message, "response_metadata", None) or {}).get("token_usage") or {}
        tokens_in = int(legacy.get("prompt_tokens", 0))
        tokens_out = int(legacy.get("completion_tokens", 0))
    if not tokens_in and not tokens_out:
        # Say so. A completed call always spent input tokens, so zero here is
        # not a cheap call - it is accounting that has stopped working, and it
        # is silent all the way to the screen: every row costs $0.000 and
        # `/runs` reports a month of spend as nothing while the money leaves.
        # `pricing.price_for` makes the same noise for a model it cannot price,
        # for the same reason - a surprise in the billing dashboard is worse
        # than a bad number in ours.
        log.warning("no token usage on a %s; this call is costed at zero", type(message).__name__)
    return Usage(tokens_in, tokens_out)


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


# Each factory takes an optional model, and the settings value is what it falls
# back to (ADR 0020). The environment still decides what a plain run uses; it no
# longer decides what *this* run uses, because the caller may have been handed a
# choice at the press or on the command line.
def summarizer(settings: Settings | None = None, model: str | None = None) -> ChatOpenAI:
    settings = settings or get_settings()
    return make_llm(model or settings.openai_model_summarize, settings, temperature=0.2)


def ranker(settings: Settings | None = None, model: str | None = None) -> ChatOpenAI:
    settings = settings or get_settings()
    return make_llm(model or settings.openai_model, settings, temperature=0.1)


def judge(settings: Settings | None = None, model: str | None = None) -> ChatOpenAI:
    """The grounding judge (`ainews eval judge`): a stronger tier of the same
    vendor, at temperature 0 so a re-run judges the same way. Never called by
    the pipeline - only the evals layer constructs it (ADR 0019)."""
    settings = settings or get_settings()
    return make_llm(model or settings.openai_model_judge, settings, temperature=0)
