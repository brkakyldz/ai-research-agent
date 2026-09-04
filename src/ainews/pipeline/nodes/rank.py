"""The rank step: one LLM call that reads the whole day at once.

The importance scores from the summarize step were each assigned in isolation -
one article, no idea what else happened that day - so five separate 4s are common
and mean nothing relative to each other. This node is the only place with the
whole day in view, which is what lets it say "these three are the same event" and
"this 4 is really today's 5".

The model is asked for candidate *numbers*, not article ids. Ids are long, easy
to transpose and carry no meaning; short ordinals that only exist inside one
prompt are much harder to get subtly wrong, and validating them is a range check.

If the call fails the digest still ships: the fallback ordering is importance
first, source weight second, which is what a human would do with the same table.
"""

from __future__ import annotations

import logging

from ainews.config import Settings, get_settings
from ainews.pipeline.llm import ranker, usage_from_message
from ainews.pipeline.prompts import load_prompt
from ainews.pipeline.state import RankedDigest, SummaryPayload

log = logging.getLogger(__name__)

FALLBACK_NOTE = {
    "tr": "Sıralama modelsiz yapıldı: önem puanı, sonra kaynak ağırlığı.",
    "en": "Ranked without the model: importance score first, source weight second.",
}


def _fallback_order(
    summaries: list[SummaryPayload], weights: dict[int, float], top_n: int
) -> list[int]:
    ordered = sorted(
        summaries,
        key=lambda s: (s["importance"], weights.get(s["article_id"], 1.0)),
        reverse=True,
    )
    return [s["article_id"] for s in ordered[:top_n]]


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
) -> tuple[list[int], str, int, int]:
    """Return (ordered article ids, editor's note, tokens in, tokens out)."""
    settings = settings or get_settings()
    weights = {aid: weight for aid, (_, weight) in meta.items()}
    top_n = min(settings.digest_top_n, len(summaries))
    if not summaries:
        return [], "", 0, 0

    prompt = load_prompt("rank", language).format(
        top_n=top_n,
        candidates=build_candidate_table(summaries, meta),
    )

    try:
        model = ranker(settings).with_structured_output(RankedDigest, include_raw=True)
        response = await model.ainvoke(prompt)
        parsed = response.get("parsed") if isinstance(response, dict) else None
        if parsed is None:
            raise ValueError("ranker returned unparsable output")
    except Exception as exc:
        log.warning("ranking failed (%s); falling back to importance order", exc)
        return _fallback_order(summaries, weights, top_n), FALLBACK_NOTE.get(language, ""), 0, 0

    usage = usage_from_message(response.get("raw"))

    # The model answers with ordinals from the prompt, so anything out of range
    # or repeated is dropped rather than trusted.
    seen: set[int] = set()
    ordered: list[int] = []
    for number in parsed.order:
        index = number - 1
        if 0 <= index < len(summaries) and index not in seen:
            seen.add(index)
            ordered.append(summaries[index]["article_id"])

    if not ordered:
        log.warning("ranker returned no usable positions; falling back to importance order")
        ordered = _fallback_order(summaries, weights, top_n)

    return ordered[:top_n], parsed.editor_note.strip(), usage.tokens_in, usage.tokens_out
