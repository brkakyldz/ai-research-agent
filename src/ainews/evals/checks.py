"""Deterministic checks over a run's stored output.

Every function takes plain dicts and returns plain numbers: no I/O, no settings,
no model. That is what lets the same code run over a recorded fixture inside
`pytest` and over the live database inside `ainews eval report`.

A story dict has the shape `record.py` writes: `article_id`, `source`, `weight`,
`title`, `title_local`, `summary`, `why_it_matters`, `tags`, `importance`,
`rank`, `body_numerals`, `body_capitalised`. Article bodies are never here -
only the numerals and capitalised tokens a check needs.

The 2026-09-04 probe that these grew out of is
`reports/research/2026-09-05_quality-evaluation.md`: a ten-line numeral check
over 91 summaries found one fabricated figure and no false positives, and both
prompt regressions in the project's history were format failures a word count
would have caught.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from ainews.pipeline.nodes.dedupe import normalize_title, titles_match
from ainews.pipeline.prompts import TAG_VOCABULARY

Story = dict[str, Any]

# The budgets the summarize prompt states, in both languages.
WORD_BUDGET = {"summary": 55, "why_it_matters": 20, "title_local": 10}

# -- numerals -----------------------------------------------------------------

# A number with optional grouping and decimal marks, optionally glued to a
# currency or percent sign on either side, optionally followed by a scale word
# or a one-letter suffix ("3B", "300M"). Turkish and English scale words both.
_SCALE = {
    "bin": 1_000,
    "thousand": 1_000,
    "k": 1_000,
    "milyon": 1_000_000,
    "million": 1_000_000,
    "m": 1_000_000,
    "mn": 1_000_000,
    "milyar": 1_000_000_000,
    "billion": 1_000_000_000,
    "b": 1_000_000_000,
    "bn": 1_000_000_000,
    "trilyon": 1_000_000_000_000,
    "trillion": 1_000_000_000_000,
}
_NUMERAL = re.compile(
    r"(?<![\w.])"
    r"[$€£₺%]?\s?"
    r"(?P<number>\d+(?:[.,]\d+)*)"
    r"\s?%?"
    r"(?:\s?(?P<scale>bin|thousand|milyon|million|mn|milyar|billion|bn|trilyon|trillion|[kmb])"
    r"(?=[^\w]|$))?",
    re.IGNORECASE,
)
# Turkish suffixes glue to the number with an apostrophe: "500'den", "2026'da".
# They carry no numeric meaning and are simply not part of the match.


def _parse_number(raw: str) -> float | None:
    """`12,9` and `12.9` are both twelve point nine; `1,000` and `1.000` are both
    a thousand. A mark followed by exactly three digits, with nothing after or
    another such group, is a thousands separator; anything else is a decimal.
    """
    parts = re.split(r"([.,])", raw)
    digits = parts[0::2]
    marks = parts[1::2]
    if not marks:
        return float(digits[0])
    if all(len(d) == 3 for d in digits[1:]):
        # Every group after the first has three digits: grouping marks, unless
        # there is exactly one mark and the whole thing is short ("1,500" is a
        # thousand and a half; "1,5" never reaches here).
        return float("".join(digits))
    if len(marks) == 1:
        return float(digits[0] + "." + digits[1])
    # Mixed, e.g. "1.234,5": the last mark is the decimal one.
    return float("".join(digits[:-1]) + "." + digits[-1])


def _canonical(value: float) -> str:
    """One spelling per value, so `12.9e9` and `12900000000.0` compare equal."""
    if value == int(value):
        return str(int(value))
    return repr(round(value, 6))


def numeral_values(text: str) -> set[str]:
    """Every number in `text`, canonicalised.

    A number with a scale word yields *both* its scaled value and its bare
    value: "13 billion" is `{"13000000000", "13"}`. The bare form is kept so a
    summary that says "13" about a body that says "13 billion" is still
    grounded; the scaled form is what lets "12,9 milyar" meet "$12.9 billion".
    """
    values: set[str] = set()
    for match in _NUMERAL.finditer(text or ""):
        number = _parse_number(match.group("number"))
        if number is None:
            continue
        values.add(_canonical(number))
        scale = match.group("scale")
        if scale:
            values.add(_canonical(number * _SCALE[scale.lower()]))
    return values


def ungrounded_numerals(stories: list[Story]) -> list[tuple[int, list[str]]]:
    """`(article_id, numerals)` for every story whose summary or why-it-matters
    carries a number the body did not.

    A number with a scale word is checked in its scaled form only - "13 milyar"
    against a body that says "12.9 billion" is a rounded figure, and the prompt
    says a rounded figure is a figure the text did not give.
    """
    flagged: list[tuple[int, list[str]]] = []
    for story in stories:
        body = set(story.get("body_numerals") or [])
        if not body:
            # A body-less article was summarised from its title; nothing to
            # ground against, so nothing to flag.
            continue
        text = f"{story.get('summary', '')} {story.get('why_it_matters', '')}"
        missing: list[str] = []
        for match in _NUMERAL.finditer(text):
            number = _parse_number(match.group("number"))
            if number is None:
                continue
            scale = match.group("scale")
            value = _canonical(number * _SCALE[scale.lower()]) if scale else _canonical(number)
            if value not in body and value not in missing:
                missing.append(value)
        if missing:
            flagged.append((int(story["article_id"]), missing))
    return flagged


# -- format budgets -----------------------------------------------------------

_SENTENCE_END = re.compile(r"[.!?…](?:\s|$)")


def word_count(text: str) -> int:
    return len((text or "").split())


def sentence_count(text: str) -> int:
    return len(_SENTENCE_END.findall((text or "").strip())) or (1 if (text or "").strip() else 0)


def word_budget(stories: list[Story]) -> dict[str, Any]:
    """Share of stories over each field's word budget, and the summary's
    sentence-count histogram."""
    n = len(stories)
    over = dict.fromkeys(WORD_BUDGET, 0)
    sentences: Counter[int] = Counter()
    for story in stories:
        for field, budget in WORD_BUDGET.items():
            if word_count(story.get(field, "")) > budget:
                over[field] += 1
        sentences[sentence_count(story.get("summary", ""))] += 1
    return {
        "n": n,
        "over_share": {field: (count / n if n else 0.0) for field, count in over.items()},
        "over_count": over,
        "sentences": dict(sorted(sentences.items())),
    }


# -- tags and scores ----------------------------------------------------------


def tag_vocabulary(stories: list[Story], top: int = 10) -> dict[str, Any]:
    """How much of a filter the tags make.

    `singleton_share` is the share of distinct tags used exactly once - a tag
    that filters to one story filters nothing. `in_vocabulary_share` is the
    share of tag *uses* drawn from the list the prompt asks the model to prefer
    (`prompts.TAG_VOCABULARY`); on a run recorded before that list existed it
    reports how the model's own words happened to overlap it.
    """
    counts: Counter[str] = Counter()
    for story in stories:
        counts.update(story.get("tags") or [])
    distinct = len(counts)
    singletons = sum(1 for c in counts.values() if c == 1)
    uses = sum(counts.values())
    in_vocabulary = sum(n for tag, n in counts.items() if tag in TAG_VOCABULARY)
    return {
        "distinct": distinct,
        "singleton_share": (singletons / distinct) if distinct else 0.0,
        "in_vocabulary_share": (in_vocabulary / uses) if uses else 0.0,
        "top": counts.most_common(top),
    }


def importance_distribution(stories: list[Story]) -> dict[int, float]:
    n = len(stories)
    counts = Counter(int(s.get("importance", 0)) for s in stories)
    return {score: (counts.get(score, 0) / n if n else 0.0) for score in range(1, 6)}


# -- ranking ------------------------------------------------------------------


def fallback_order(stories: list[Story], top_n: int) -> list[int]:
    """What `rank.py` does without the model: importance, then source weight."""
    ordered = sorted(
        stories,
        key=lambda s: (int(s.get("importance", 0)), float(s.get("weight", 1.0))),
        reverse=True,
    )
    return [int(s["article_id"]) for s in ordered[:top_n]]


def ranked_order(stories: list[Story]) -> list[int]:
    ranked = [s for s in stories if s.get("rank") is not None]
    return [int(s["article_id"]) for s in sorted(ranked, key=lambda s: int(s["rank"]))]


def ranker_vs_fallback(stories: list[Story], top_n: int | None = None) -> dict[str, Any]:
    """How much the rank call changed against the free ordering.

    Reported, never asserted: an overlap equal to `top_n` on most runs means the
    rank call buys nothing (E5 trigger, "delete the rank call"); a low one means
    the model is doing something, which is not the same as doing it well.
    """
    ranked = ranked_order(stories)
    n = top_n if top_n is not None else len(ranked)
    fallback = fallback_order(stories, n)
    overlap = len(set(ranked[:n]) & set(fallback))
    return {"top_n": n, "overlap": overlap, "ranked": ranked[:n], "fallback": fallback}


def editor_shift(stories: list[Story]) -> dict[str, Any]:
    """How far the ranker moved the summariser's scores on the stories it kept.

    Reported, never asserted. The rank call is asked to correct the isolated
    scores where the day makes them wrong, and ADR 0025 is what lets those
    corrections reach the page. This says whether it uses the power: a
    `n_changed` of zero on every run means the prompt line is decoration, and
    a mean shift near two means the summariser's scale and the ranker's are not
    the same scale. Stories with no `editor_importance` - unranked, or from a
    run before the column existed - are not counted.
    """
    shifts = [
        int(s["editor_importance"]) - int(s.get("importance", 0))
        for s in stories
        if s.get("rank") is not None and s.get("editor_importance") is not None
    ]
    changed = [d for d in shifts if d]
    return {
        "n_ranked": len(shifts),
        "n_changed": len(changed),
        "up": sum(1 for d in changed if d > 0),
        "down": sum(1 for d in changed if d < 0),
        "mean_abs_shift": (sum(abs(d) for d in changed) / len(shifts)) if shifts else 0.0,
    }


def unrepresented_fives(stories: list[Story], threshold: int = 85) -> list[int]:
    """Importance-5 stories with no ranked story in their cluster.

    A cluster is what the dedupe step would have merged: two titles that
    `titles_match` under the production threshold. A five that the ranker
    passed over in favour of another outlet's write-up of the same event is
    represented; a five nobody in its cluster carries into the digest is not.
    """
    ranked = [s for s in stories if s.get("rank") is not None]
    ranked_titles = [normalize_title(s.get("title") or "") for s in ranked]
    missing: list[int] = []
    for story in stories:
        if int(story.get("importance", 0)) != 5 or story.get("rank") is not None:
            continue
        title = normalize_title(story.get("title") or "")
        if any(titles_match(title, other, threshold) for other in ranked_titles):
            continue
        missing.append(int(story["article_id"]))
    return missing


# -- the editor's note --------------------------------------------------------


def editor_note_shape(note: str | None) -> dict[str, Any]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", (note or "").strip()) if p.strip()]
    return {"paragraphs": len(paragraphs), "words": [word_count(p) for p in paragraphs]}
