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
    # The scale itself - what a 5 is, what a 2 is - is written in the prompt
    # (`prompts/*/summarize.md`) and nowhere else. It was here as well until
    # 2026-09-08, and the JSON schema reaches the model beside the prompt, so
    # the rubric was stated twice to the same reader and could drift apart.
    importance: int = Field(ge=1, le=5, description="1 to 5, on the scale the instructions define.")

    @field_validator("tags", mode="after")
    @classmethod
    def _tidy_tags(cls, tags: list[str]) -> list[str]:
        seen: list[str] = []
        for tag in tags:
            cleaned = tag.strip().lower().replace(" ", "-")
            if cleaned and cleaned not in seen:
                seen.append(cleaned)
        return seen[:4]


class Pick(BaseModel):
    """One story the ranker keeps, and what it thinks the story is worth.

    The importance travels with the pick because the ranker is the one node
    that sees the whole day, and the prompt has asked it since 2026-09-05 to
    correct the summariser's isolated scores where the day makes them wrong.
    Until 2026-09-08 the schema had nowhere to put the correction: the model was
    told to fix a number and could only return an order, and the page then
    sorted by the number it had been told to fix (ADR 0025).
    """

    number: int = Field(description="The candidate number shown in the table.")
    importance: int = Field(
        ge=1,
        le=5,
        description=(
            "The story's importance in the context of the whole day, on the same 1-5 "
            "scale the summariser used. Keep the summariser's score when the day "
            "confirms it; change it when the day contradicts it."
        ),
    )


class RankedDigest(BaseModel):
    """What the rank node must return for a whole run."""

    # A word count, not a sentence count. "One or two sentences" was the ask
    # until 2026-09-05 and the model answered with whatever length it liked;
    # a number of words is a constraint it actually honours.
    editor_note: str = Field(
        description=(
            "Three paragraphs separated by a blank line, 25-40 words each: what led "
            "today, the day's other thread, what it means for someone building this "
            "week. No greeting, no headings, no bullets."
        )
    )
    picks: list[Pick] = Field(
        description=(
            "The stories that belong in the digest, most important first, each with "
            "its candidate number and its importance in the day's context."
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
    # The ranker's read of the story against the day. `persist` writes it to
    # `summaries.editor_importance`; the summariser's own score stays in
    # `importance`, because the evaluation layer compares the two.
    importance: int


class RankUsage(TypedDict):
    """The rank call's tokens, on their own channel.

    Until 2026-09-08 these rode back on a fake `SummaryPayload` with
    `article_id = -1`, "to avoid opening a second channel for two integers".
    The carrier then had to be defined in two modules, filtered in three, and
    explained in each - which is more than a channel costs.
    """

    tokens_in: int
    tokens_out: int


class PipelineState(TypedDict, total=False):
    """The digest graph's state.

    `summaries` and `errors` are reducer fields: the summarize fan-out writes one
    branch per article and LangGraph concatenates them, which is the only reason
    a hundred parallel branches can share one key without clobbering each other.

    The state carries what a node downstream reads and nothing else. `mode` was
    a key here until 2026-09-08; no node ever read it - the run row's `kind`
    already says what started the run - so it came off.
    """

    run_id: str
    language: Language
    # Which model each paid node runs, decided at the press or on the command
    # line and carried here rather than read from settings inside the node
    # (ADR 0020). In the state and not in a module global because two of these
    # nodes run a hundred branches wide: a global would be one value for a
    # process, and this has to be one value for a *run*. `persist` prices the
    # run off these two names, so the run row cannot disagree with what ran -
    # and a resumed run (`runner.run_digest(resume=...)`) reads them back off
    # the checkpoint, so it is finished by the models that started it.
    model_summarize: str
    model_rank: str
    candidate_ids: list[int]
    summaries: Annotated[list[SummaryPayload], operator.add]
    ranked: list[RankedItem]
    rank_usage: RankUsage
    editor_note: str
    errors: Annotated[list[str], operator.add]
    n_collected: int
    n_new: int


class SummarizeTask(TypedDict, total=False):
    """Payload of a single `Send` into the summarize node.

    `model` is optional so a checkpoint written before ADR 0020 can still be
    resumed: the node falls back to the configured summariser when the key is
    absent, which is what that run was using anyway.
    """

    run_id: str
    language: Language
    article_id: int
    model: str
