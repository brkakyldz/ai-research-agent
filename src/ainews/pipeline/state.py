"""Graph state and the schemas the LLM is held to.

Two rules shape this module.

State carries **ids, not bodies**. LangGraph writes a checkpoint after every
superstep, so anything in the state is serialised once per step per branch; a
hundred article bodies in there would turn a cheap run into a slow one. The
bodies stay in SQLite and the graph passes primary keys.

The LLM's output is a **Pydantic model, not a prompt convention**. Both LLM nodes
use `with_structured_output`, so a malformed answer fails as a validation error
at the node that produced it rather than as a `KeyError` three nodes later.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field, field_validator

Language = Literal["tr", "en"]
RunMode = Literal["digest", "manual"]


class ArticleSummary(BaseModel):
    """What the summarize node must return for one article."""

    title_local: str = Field(
        description="The headline rewritten in the target language. Plain, factual, no clickbait."
    )
    summary: str = Field(
        description="Exactly three sentences covering what happened, by whom, and what is new."
    )
    why_it_matters: str = Field(
        description="One sentence on the consequence for someone building with AI."
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Two to four lowercase topic tags, English, e.g. ['openai', 'agents'].",
    )
    importance: int = Field(
        ge=1,
        le=5,
        description=(
            "5 = a major lab ships or announces something that changes what is buildable. "
            "4 = significant release, funding or research result. 3 = worth knowing. "
            "2 = incremental. 1 = noise, opinion or rehash."
        ),
    )

    @field_validator("tags", mode="after")
    @classmethod
    def _tidy_tags(cls, tags: list[str]) -> list[str]:
        seen: list[str] = []
        for tag in tags:
            cleaned = tag.strip().lower().replace(" ", "-")
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
        return seen[:4]


class RankedDigest(BaseModel):
    """What the rank node must return for a whole run."""

    editor_note: str = Field(
        description="One or two sentences naming the through-line of the day. No greeting."
    )
    order: list[int] = Field(
        description=(
            "The candidate numbers shown in the prompt, most important first. "
            "Include only the ones that belong in the digest."
        )
    )


class SummaryPayload(TypedDict):
    """One finished summary on its way back through the `Send` reducer."""

    article_id: int
    title_local: str
    summary: str
    why_it_matters: str
    tags: list[str]
    importance: int
    tokens_in: int
    tokens_out: int


class RankedItem(TypedDict):
    article_id: int
    rank: int


class PipelineState(TypedDict, total=False):
    """The digest graph's state.

    `summaries` and `errors` are reducer fields: the summarize fan-out writes one
    branch per article and LangGraph concatenates them, which is the only reason
    a hundred parallel branches can share one key without clobbering each other.
    """

    run_id: str
    language: Language
    mode: RunMode
    candidate_ids: list[int]
    summaries: Annotated[list[SummaryPayload], operator.add]
    ranked: list[RankedItem]
    editor_note: str
    errors: Annotated[list[str], operator.add]
    n_collected: int
    n_new: int


class SummarizeTask(TypedDict):
    """Payload of a single `Send` into the summarize node."""

    run_id: str
    language: Language
    article_id: int
