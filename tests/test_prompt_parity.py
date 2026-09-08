"""The two languages' prompts have to promise the same thing.

They are two files maintained by hand, and the only test on them was that both
exist — so the TR and EN summarise prompts had already drifted apart by six
lines before anyone counted.

They are not merged into one body with a language clause, and that is a
decision. The Turkish prompt carries rules that have no English counterpart and
should not have one: do not force-translate `model`, `agent`, `open-source`; the
tags stay English because they are filter keys and a filter that changes with the
interface language filters nothing. A single body would have to state those to a
reader they do not apply to. What is merged is the *contract* — the placeholders
each prompt is formatted with, the fields it names, and the limits `evals/checks`
measures against — because those are what the app and the evaluation depend on,
and they are what silently diverges.

A prompt is prose and drifting prose is fine. A prompt whose word cap differs by
language is two different products.
"""

from __future__ import annotations

import re

import pytest

from ainews.pipeline.prompts import PROMPT_DIR, available_languages, load_prompt

LANGUAGES = available_languages()
PARALLEL = ("summarize", "rank")

PLACEHOLDER = re.compile(r"\{(\w+)\}")
# Backticked identifiers: the schema fields a prompt tells the model to fill.
FIELD = re.compile(r"`(\w+)`")
NUMBER = re.compile(r"\b(\d+)\b")


def _read(name: str, language: str) -> str:
    return load_prompt(name, language)


def test_both_languages_are_present() -> None:
    assert set(LANGUAGES) >= {"tr", "en"}


@pytest.mark.parametrize("name", PARALLEL)
def test_every_language_has_the_prompt(name: str) -> None:
    for language in LANGUAGES:
        assert (PROMPT_DIR / language / f"{name}.md").is_file()


@pytest.mark.parametrize("name", PARALLEL)
def test_the_placeholders_match(name: str) -> None:
    """`load_prompt(...).format(**kwargs)` is called with one set of arguments
    whatever the language. A placeholder in one file and not the other is either
    a `KeyError` at the top of a paid run or a `{title}` printed literally to the
    model."""
    sets = {lang: set(PLACEHOLDER.findall(_read(name, lang))) for lang in LANGUAGES}
    reference = sets["en"]
    for language, found in sets.items():
        assert found == reference, f"{name}/{language} placeholders differ: {found ^ reference}"


@pytest.mark.parametrize("name", PARALLEL)
def test_the_fields_named_match(name: str) -> None:
    """The structured-output schema is one Pydantic model for both languages, so
    a field one prompt asks for and the other does not is a field the model is
    told about in one language only."""
    sets = {lang: set(FIELD.findall(_read(name, lang))) for lang in LANGUAGES}
    reference = sets["en"]
    for language, found in sets.items():
        assert found == reference, f"{name}/{language} names differ: {found ^ reference}"


def test_the_summarisers_limits_are_the_same_number_in_both() -> None:
    """Three sentences, 55 words, 20 words, 1-5, 10 words. `evals/checks` measures
    against these exact figures and does not know which language it is reading, so
    a cap that differs by language makes one of the two languages fail a check it
    was never told about."""
    for limit in ("55", "20", "10", "5"):
        for language in LANGUAGES:
            body = _read("summarize", language)
            assert limit in NUMBER.findall(body), f"summarize/{language} lost the limit {limit}"


def test_the_ranker_asks_for_the_same_shape_in_both() -> None:
    """ADR 0025 gave the ranker `picks` of `{number, importance}`. A prompt still
    asking for a bare order would return a `RankedDigest` with no corrected
    scores, and the page draws its headline sizes from those."""
    for language in LANGUAGES:
        body = _read("rank", language)
        for field in ("number", "importance", "editor_note", "top_n"):
            assert field in body, f"rank/{language} does not mention {field}"


@pytest.mark.parametrize("name", PARALLEL)
def test_the_rule_lists_are_the_same_length_or_the_difference_is_declared(name: str) -> None:
    """The one place divergence is allowed, and it has to be declared here.

    Turkish carries one rule English has no use for: do not force-translate
    `model`, `agent`, `open-source`, `fine-tune`. The other Turkish-only
    sentence - that the tags stay English because they are filter keys, and a
    filter that changes with the interface language filters nothing - is a
    rationale inside the shared tags rule rather than a rule of its own.

    Anything beyond that is drift, and drift in a rule list is a rule one
    language's bulletin follows and the other does not. Adding a rule to one file
    fails this until the same rule reaches the other, or until the exception is
    written down here with its reason.
    """
    allowed = {"summarize": {"tr": 1}, "rank": {"tr": 0}}[name]
    counts = {
        lang: sum(1 for line in _read(name, lang).splitlines() if line.startswith("- "))
        for lang in LANGUAGES
    }
    for language, n in counts.items():
        expected = counts["en"] + allowed.get(language, 0)
        assert n == expected, (
            f"{name}/{language} has {n} rules against English's {counts['en']}; "
            f"{allowed.get(language, 0)} extra are declared here"
        )
