"""The rank step: one LLM call that reads the whole day at once.

The importance scores from the summarize step were each assigned in isolation -
one article, no idea what else happened that day - so five separate 4s are common
and mean nothing relative to each other. This node is the only place with the
whole day in view, which is what lets it say "these three are the same event" and
"this 4 is really today's 5".

It answers two things per story it keeps: its place in the reading order and its
importance against the day. The second is what the page draws - the headline's
size comes from it (ADR 0025) - so a correction the ranker makes reaches the
reader instead of stopping in the prompt.

The model is asked for candidate *numbers*, not article ids. Ids are long, easy
to transpose and carry no meaning; short ordinals that only exist inside one
prompt are much harder to get subtly wrong, and validating them is a range check.

If the call fails the digest still ships: the fallback ordering is importance
first, source weight second, which is what a human would do with the same table,
and every kept story keeps the summariser's score.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ainews.config import Settings, get_settings
from ainews.pipeline.llm import ranker, usage_from_message
from ainews.pipeline.prompts import load_prompt
from ainews.pipeline.state import RankedDigest, SummaryPayload

log = logging.getLogger(__name__)

FALLBACK_NOTE = {
    "tr": "Sıralama modelsiz yapıldı: önem puanı, sonra kaynak ağırlığı.",
    "en": "Ranked without the model: importance score first, source weight second.",
}


@dataclass(slots=True)
class Ranking:
    """What the rank call produced, or what stood in for it.

    `order` is the article ids in reading order; `importance` is the ranker's
    score for each of them. The two are kept apart rather than zipped because
    they are read apart: the stability probe compares orders, `persist` writes
    scores.
    """

    order: list[int] = field(default_factory=list)
    importance: dict[int, int] = field(default_factory=dict)
    editor_note: str = ""
    tokens_in: int = 0
    tokens_out: int = 0


def _fallback(
    summaries: list[SummaryPayload], weights: dict[int, float], top_n: int, language: str
) -> Ranking:
    ordered = sorted(
        summaries,
        key=lambda s: (s["importance"], weights.get(s["article_id"], 1.0)),
        reverse=True,
    )[:top_n]
    return Ranking(
        order=[s["article_id"] for s in ordered],
        importance={s["article_id"]: s["importance"] for s in ordered},
        editor_note=FALLBACK_NOTE.get(language, ""),
    )


def build_candidate_table(
    summaries: list[SummaryPayload], meta: dict[int, tuple[str, float]]
) -> str:
    lines = []
    for number, item in enumerate(summaries, start=1):
        source, weight = meta.get(item["article_id"], ("unknown", 1.0))
        lines.append(
            f"{number}. [{source} · weight {weight:.1f} · importance {item['importance']}] "
            f"{item['title_local']}\n   {item['summary']}"
        )
    return "\n\n".join(lines)


async def rank_summaries(
    summaries: list[SummaryPayload],
    meta: dict[int, tuple[str, float]],
    language: str,
    settings: Settings | None = None,
    model: str | None = None,
) -> Ranking:
    settings = settings or get_settings()
    weights = {aid: weight for aid, (_, weight) in meta.items()}
    top_n = min(settings.digest_top_n, len(summaries))
    if not summaries:
        return Ranking()

    prompt = load_prompt("rank", language).format(
        top_n=top_n,
        candidates=build_candidate_table(summaries, meta),
    )

    try:
        client = ranker(settings, model).with_structured_output(RankedDigest, include_raw=True)
        response = await client.ainvoke(prompt)
        parsed = response.get("parsed") if isinstance(response, dict) else None
        if parsed is None:
            raise ValueError("ranker returned unparsable output")
    except Exception as exc:
        log.warning("ranking failed (%s); falling back to importance order", exc)
        return _fallback(summaries, weights, top_n, language)

    usage = usage_from_message(response.get("raw"))

    # The model answers with ordinals from the prompt, so anything out of range
    # or repeated is dropped rather than trusted. The importance on a pick is
    # already 1-5 by the schema.
    seen: set[int] = set()
    ranking = Ranking(
        editor_note=parsed.editor_note.strip(),
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
    )
    for pick in parsed.picks:
        index = pick.number - 1
        if 0 <= index < len(summaries) and index not in seen:
            seen.add(index)
            article_id = summaries[index]["article_id"]
            ranking.order.append(article_id)
            ranking.importance[article_id] = pick.importance
        if len(ranking.order) == top_n:
            break

    if not ranking.order:
        log.warning("ranker returned no usable positions; falling back to importance order")
        fallback = _fallback(summaries, weights, top_n, language)
        ranking.order, ranking.importance = fallback.order, fallback.importance

    return ranking
