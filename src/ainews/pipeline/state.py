"""Graph state and the schemas the LLM is held to.

Two rules shape this module.

State carries **ids, not bodies**. A `Send` fan-out passes its state to every
branch and the reducer collects a value back from each, so anything in here is
carried a hundred times over on a hundred-article day. The bodies stay in
SQLite and the graph passes primary keys.

The LLM's output is a **Pydantic model, not a prompt convention**. Both LLM nodes
use `with_structured_output`, so a malformed answer fails as a validation error
at the node that produced it rather than as a `KeyError` three nodes later.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, Field, field_validator

Language = Literal["tr", "en"]
# What kind of item the summariser thinks it read. The ranker uses it to
# select rather than to score: a roundup is never offered the lead, because a
# newsletter covering nine things is not the day's story even when it touches
# the day's story.
SummaryKind = Literal["news", "release", "research", "opinion", "roundup", "other"]
# The four bands the page draws. A tier and not a second 1-5 score: the
# summariser is told to be honest and that most items are 2 or 3, the ranker is
# told that a 4 leading the day is a 5, and a page that sorts by
# `coalesce(editor_importance, importance)` mixes an absolute scale with a
# relative one in a layout whose only ranking indicator is size (ADR 0030).
Tier = Literal["lead", "major", "notable", "brief"]
# Hardest first. Two readers need the order - the aggregate breaks a tie in the
# tier vote downward, and the page draws a size step - so it is stated once.
TIER_ORDER: tuple[Tier, ...] = ("lead", "major", "notable", "brief")
# What each tier draws at on the summariser's own 1-5 ink scale (`theme.css`,
# `.p1`-`.p5`). Four tiers over five steps is not an oversight: `p1` is the
# bottom of that scale and belongs to stories *outside* the bulletin, where a 1
# means noise. Nothing the editor chose is noise.
TIER_STEP: dict[Tier, int] = {"lead": 5, "major": 4, "notable": 3, "brief": 2}


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
    # The one concrete thing that makes this news. It costs no extra call - it
    # is a field on an answer already being bought - and it is the only floor
    # under *content*: every other check measures shape, and three generic
    # numberless sentences at importance 3 pass every one of them, plus the
    # numeral check (nothing to flag) and the judge (nothing unsupported).
    key_fact: str = Field(
        default="",
        description=(
            "The single figure, name, version or date from the article that makes "
            "this news, copied as it appears there. Five words at most. Empty only "
            "if the article genuinely contains none."
        ),
    )
    # The source list was the only thing deciding whether an item was AI news,
    # and one weight-1.5 source is a personal blog that also publishes on map
    # projections. Asked at summarise time and not at rank time because the
    # summariser has the article in front of it and the ranker has a table.
    relevant: bool = Field(
        default=True,
        description=(
            "True if this is AI or software-industry news. False for anything the "
            "feed carries that is not - a personal essay, a hobby project, an "
            "unrelated field."
        ),
    )
    kind: SummaryKind = Field(
        default="news",
        description=(
            "What shape of item this is: news, release (a product or version "
            "shipping), research (a paper or technical result), opinion, roundup "
            "(a link digest or newsletter covering many things), other."
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


class Pick(BaseModel):
    """One story the ranker keeps, where it belongs, and why.

    `tier` and not a corrected 1-5 importance (ADR 0030). The ranker is the one
    node that sees the whole day, and what it is qualified to say is how this
    story stands against the others in front of it - not what its absolute
    significance is on a scale the summariser was given a rubric for. A tier is
    the page's own vocabulary: it is what the headline size draws.

    `reason` is stored because "prefer one strong story over three angles on it"
    was a rule with no trace. The ranker's cluster decisions left nothing behind
    for anyone to check, so the rule could only be evaluated by re-reading the
    day by hand.
    """

    number: int = Field(description="The candidate number shown in the table.")
    tier: Tier = Field(
        description=(
            "lead for the one story that leads the day, major for the few that "
            "would headline any other day, notable for solid items, brief for "
            "worth-knowing. At most one lead."
        )
    )
    reason: str = Field(
        default="",
        description=(
            "Up to fifteen words on why this story is at this tier, or why it "
            "represents its cluster. Not a summary of the story."
        ),
    )


class RankedDigest(BaseModel):
    """What the rank node must return for a whole run."""

    # A word count, not a sentence count: asked for "one or two sentences" the
    # model answers with whatever length it likes, and a number of words is a
    # constraint it actually honours.
    editor_note: str = Field(
        description=(
            "Three paragraphs separated by a blank line, 25-40 words each: what led "
            "today, the day's other thread, what it means for someone building this "
            "week. No greeting, no headings, no bullets."
        )
    )
    picks: list[Pick] = Field(
        description=(
            "The stories that belong in today's bulletin, most important first, "
            "each with its candidate number and its tier. Fewer than the maximum "
            "when the day is thin."
        )
    )


class SummaryPayload(TypedDict):
    """One committed summary on its way back through the `Send` reducer.

    Ids and money, no text. The row is already in the database when this is
    written - `summarize` commits it (ADR 0030) - so the branch carries what
    the run's bookkeeping needs and the ranker reads the day back out of SQLite.
    A hundred article summaries riding the state would be a hundred summaries in
    every message LangGraph passes between supersteps.
    """

    summary_id: int
    article_id: int
    tokens_in: int
    tokens_out: int
    est_cost_usd: float


class ItemPayload(TypedDict):
    """One story's place in the bulletin the run is about to publish."""

    summary_id: int
    position: int
    tier: Tier
    reason: str


class RankUsage(TypedDict):
    """The rank calls' tokens, on their own channel.

    Its own key rather than a fake `SummaryPayload` with `article_id = -1`
    riding the summaries channel. A sentinel that avoids "opening a second
    channel for two integers" has to be defined in two modules, filtered in
    three and explained in each, which is more than a channel costs.
    """

    tokens_in: int
    tokens_out: int


class PipelineState(TypedDict, total=False):
    """The digest graph's state.

    `summaries` and `errors` are reducer fields: the summarize fan-out writes one
    branch per article and LangGraph concatenates them, which is the only reason
    a hundred parallel branches can share one key without clobbering each other.

    The state carries what a node downstream reads and nothing else. What
    started a run is not in here, because no node asks: the run row's `kind` is
    where that lives.
    """

    run_id: str
    language: Language
    # Which model each paid node runs, decided at the press or on the command
    # line and carried here rather than read from settings inside the node
    # (ADR 0020). In the state and not in a module global because two of these
    # nodes run a hundred branches wide: a global would be one value for a
    # process, and this has to be one value for a *run*. The run row is priced
    # off these two names, so it cannot disagree with what ran.
    model_summarize: str
    model_rank: str
    candidate_ids: list[int]
    summaries: Annotated[list[SummaryPayload], operator.add]
    ranked: list[ItemPayload]
    rank_usage: RankUsage
    editor_note: str
    # The local day the bulletin belongs to, fixed when the run starts so a
    # press at 23:59 writes one day and not two. `YYYY-MM-DD`.
    day: str
    # How much the shuffled rank calls agreed with each other. Carried to
    # `persist` because it is a property of the bulletin, not of a probe.
    agreement: float
    errors: Annotated[list[str], operator.add]
    n_collected: int
    n_new: int


class SummarizeTask(TypedDict, total=False):
    """Payload of a single `Send` into the summarize node.

    `model` is optional and the node falls back to the configured summariser,
    which is what a caller that did not choose one meant.
    """

    run_id: str
    language: Language
    article_id: int
    model: str
