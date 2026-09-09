"""Deterministic checks over recorded runs (PLAN-EVALS E1.3).

Every test here runs over every fixture in `tests/fixtures/runs/`, offline. The
bounds were chosen against the first real fixture (2026-09-04, 91 stories), not
guessed, and each is a claim the prompt makes.

A fixture recorded before a fix landed cannot meet the bound that fix exists
for. Those rows are marked `xfail(strict=True)` in `KNOWN_GAPS`, so the mark
comes off loudly the day a re-recorded run meets it - and a fixture that still
does not, after the fix, fails.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ainews.evals.record import FIXTURE_DIR
from ainews.quality import checks

# fixture name -> {gap: reason}. The 2026-09-04 run predates E0.5 (the
# "use a figure only if it appears in the text" line), the three-paragraph
# editor's note (2026-09-05), and its ranked Astra lead was deleted by the
# same-day source purge (a product defect, not evaluation work).
KNOWN_GAPS: dict[str, dict[str, str]] = {
    "2026-09-04_tr.json": {
        "numerals": "predates E0.5: '500 binden fazla saat' is the figure the probe found",
        "editor_note": "predates the three-paragraph editor's-note prompt of 2026-09-05",
        "fives": "the ranked Astra lead (HN) was purged after the run; article 58 is unrepresented",
        "key_fact": "predates the field; nobody asked these articles the question",
    },
    "2026-09-06_tr.json": {
        "key_fact": "predates the field; nobody asked these articles the question",
    },
}


def _load_all() -> list[tuple[str, dict[str, Any]]]:
    return [
        (path.name, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(FIXTURE_DIR.glob("*.json"))
    ]


def _fixtures(gap: str | None = None) -> list[Any]:
    params = []
    for name, fixture in _load_all():
        reason = KNOWN_GAPS.get(name, {}).get(gap or "")
        marks = [pytest.mark.xfail(strict=True, reason=reason)] if reason else []
        params.append(pytest.param(fixture, id=name, marks=marks))
    return params


def test_there_is_at_least_one_recorded_run() -> None:
    assert _load_all(), f"no fixtures in {FIXTURE_DIR}; run `ainews eval record`"


# -- format budgets -----------------------------------------------------------


@pytest.mark.parametrize("fixture", _fixtures())
def test_summaries_keep_the_word_budget(fixture: dict[str, Any]) -> None:
    """55 / 20 / 10 words, as the prompt says. Four of ninety-one summaries ran
    over on the first run; the other two fields never did."""
    budget = checks.word_budget(fixture["stories"])
    assert budget["over_share"]["summary"] <= 0.05, budget
    assert budget["over_count"]["why_it_matters"] == 0, budget
    assert budget["over_count"]["title_local"] == 0, budget


@pytest.mark.parametrize("fixture", _fixtures())
def test_summaries_are_three_sentences(fixture: dict[str, Any]) -> None:
    sentences = checks.word_budget(fixture["stories"])["sentences"]
    n = sum(sentences.values())
    assert sentences.get(3, 0) / n >= 0.95, sentences


# -- grounding ----------------------------------------------------------------


@pytest.mark.parametrize("fixture", _fixtures("numerals"))
def test_no_numeral_appears_that_the_body_did_not_contain(fixture: dict[str, Any]) -> None:
    flagged = checks.ungrounded_numerals(fixture["stories"])
    assert flagged == [], flagged


# -- the content floor --------------------------------------------------------


def _story(**over: Any) -> dict[str, Any]:
    story = {
        "article_id": 1,
        "key_fact": "GPT-6 Astra",
        "title_local": "OpenAI GPT-6 Astra modelini duyurdu",
        "summary": "OpenAI GPT-6 Astra modelini duyurdu. 13.000 satir kod. Ucuncu cumle.",
        "why_it_matters": "Onemli.",
        "body_numerals": ["13000"],
    }
    return story | over


def test_the_content_floor_catches_prose_that_says_nothing() -> None:
    """The regression every other check passes.

    Three generic, numberless sentences at importance 3 clear the word budget,
    the sentence histogram, the tag spread and the note shape; they are flagged
    by `ungrounded_numerals` never (no figure to flag) and by the grounding
    judge never (no claim to disagree with). The 2026-09-06 run was already 70%
    threes, so this is the direction the writing drifts in (PLAN-V2 5.6).
    """
    empty = _story(
        title_local="Yeni bir gelisme",
        summary="Sirket bir gelisme duyurdu. Sektor icin onemli olabilir. Takip edilmeli.",
    )
    assert checks.ungrounded_numerals([empty]) == [], "nothing invented, so nothing to flag"

    floor = checks.content_floor([empty])
    assert floor["key_fact_kept"] == 0.0, "the model knew what the story was and did not say it"
    assert floor["numeral_recall"] == 0.0, "and it dropped the one figure the body had"

    assert checks.content_floor([_story()]) == {
        "n": 1,
        "key_fact_share": 1.0,
        "key_fact_kept": 1.0,
        "n_with_numerals": 1,
        "numeral_recall": 1.0,
    }


def test_a_key_fact_counts_as_kept_when_it_comes_back_inflected() -> None:
    """The fact is copied from an English body into a Turkish summary.

    Demanding the string back unchanged would measure the language rather than
    the writing: "3,5 milyar dolarlik" is "$3.5 billion" written in Turkish.
    """
    story = _story(
        key_fact="3.5 milyar dolar",
        title_local="Nscale yatirim aldi",
        summary="Nscale 3,5 milyar dolarlik bir tur kapatti. Iki. Uc.",
        body_numerals=["3500000000"],
    )
    assert checks.content_floor([story])["key_fact_kept"] == 1.0


def test_a_story_with_no_key_fact_is_not_counted_against_the_writing() -> None:
    """An opinion piece has no figure or version in it, and the prompt says to
    leave the field empty rather than invent one. The share is reported so a run
    of empties is visible; the kept rate is asked only of the ones that have
    one, and is `None` when none do."""
    floor = checks.content_floor([_story(key_fact=""), _story(key_fact="  ")])
    assert floor["key_fact_share"] == 0.0
    assert floor["key_fact_kept"] is None


def test_numeral_recall_asks_only_the_stories_that_can_answer() -> None:
    floor = checks.content_floor([_story(body_numerals=[])])
    assert floor["n_with_numerals"] == 0
    assert floor["numeral_recall"] is None


@pytest.mark.parametrize("fixture", _fixtures("key_fact"))
def test_most_stories_name_the_thing_that_made_them_news(fixture: dict[str, Any]) -> None:
    """Both fixtures on record fail this, and that is the point of writing it.

    They were recorded before the field existed, so the bound has nothing to
    read and the gap is counted rather than assumed away. The first run recorded
    after PLAN-V2 5.6 is the first measurement of the content floor anywhere.
    """
    floor = checks.content_floor(fixture["stories"])
    assert floor["key_fact_share"] >= 0.7, "a day is mostly releases, results and figures"
    assert (floor["key_fact_kept"] or 0.0) >= 0.8, "and the writing keeps what it found"


# -- tags and scores ----------------------------------------------------------


@pytest.mark.parametrize("fixture", _fixtures())
def test_tags_are_a_vocabulary_not_a_long_tail(fixture: dict[str, Any]) -> None:
    """67% of tags were used once on the first run. Above 70% the tag row on the
    page stops being a filter and becomes a list of every story's own words."""
    vocabulary = checks.tag_vocabulary(fixture["stories"])
    assert vocabulary["singleton_share"] <= 0.70, vocabulary


@pytest.mark.parametrize("fixture", _fixtures())
def test_importance_is_mostly_two_or_three(fixture: dict[str, Any]) -> None:
    """The prompt says most items are 2 or 3 and that 5 is rare."""
    share = checks.importance_distribution(fixture["stories"])
    assert share[2] + share[3] >= 0.50, share
    assert share[5] <= 0.10, share


# -- ranking ------------------------------------------------------------------


@pytest.mark.parametrize("fixture", _fixtures("fives"))
def test_every_five_has_a_ranked_representative(fixture: dict[str, Any]) -> None:
    assert checks.unrepresented_fives(fixture["stories"]) == []


@pytest.mark.parametrize("fixture", _fixtures())
def test_the_ranker_is_reported_against_the_fallback(fixture: dict[str, Any]) -> None:
    """A diagnostic, not a bound (E5: overlap at top_n on most runs means the
    rank call buys nothing). Here it only has to be computable."""
    result = checks.ranker_vs_fallback(fixture["stories"])
    assert 0 <= result["overlap"] <= result["top_n"]


# -- the editor's note --------------------------------------------------------


@pytest.mark.parametrize("fixture", _fixtures("editor_note"))
def test_the_editor_note_is_three_paragraphs_of_25_to_40_words(fixture: dict[str, Any]) -> None:
    shape = checks.editor_note_shape(fixture["editor_note"])
    assert shape["paragraphs"] == 3, shape
    assert all(25 <= words <= 40 for words in shape["words"]), shape


# -- the copyright rule -------------------------------------------------------


@pytest.mark.parametrize("fixture", _fixtures())
def test_the_fixture_carries_no_article_bodies(fixture: dict[str, Any]) -> None:
    """The repository is public; an article body is someone else's text."""
    for story in fixture["stories"]:
        assert "body" not in story and "body_text" not in story
        for key, value in story.items():
            if isinstance(value, str):
                assert len(value) < 1000, f"{key} on {story['article_id']} is body-sized"


def test_fixture_files_are_small(tmp_path: Path) -> None:
    for path in FIXTURE_DIR.glob("*.json"):
        assert path.stat().st_size < 250_000, path


# -- the numeral normaliser ---------------------------------------------------


@pytest.mark.parametrize(
    ("turkish", "english"),
    [
        ("12,9 milyar", "12.9 billion"),
        ("500 bin", "500000"),
        ("12,93 milyar dolar", "$12.93 billion"),
        ("3 milyar", "$3B"),
        ("300 milyon", "300M"),
        ("1.000 kişi", "1,000 people"),
        ("%50", "50%"),
        ("2026'da", "in 2026"),
    ],
)
def test_turkish_and_english_figures_meet(turkish: str, english: str) -> None:
    assert checks.numeral_values(turkish) & checks.numeral_values(english)


def test_a_scaled_figure_is_checked_in_its_scaled_form() -> None:
    """ "13 milyar" against a body that says "12.9 billion" is a rounded figure,
    and the prompt says a rounded figure is one the text did not give."""
    body = sorted(checks.numeral_values("Nvidia will pay $12.9 billion for it."))
    story = {
        "article_id": 1,
        "summary": "13 milyar dolar ödeyecek.",
        "why_it_matters": "",
        "body_numerals": body,
    }
    assert checks.ungrounded_numerals([story]) == [(1, ["13000000000"])]


def test_a_bare_figure_is_grounded_by_its_scaled_twin() -> None:
    body = sorted(checks.numeral_values("The round was $300 million."))
    story = {"article_id": 1, "summary": "300 alındı.", "why_it_matters": "", "body_numerals": body}
    assert checks.ungrounded_numerals([story]) == []


def test_a_bodyless_article_flags_nothing() -> None:
    story = {"article_id": 1, "summary": "500 bin saat.", "why_it_matters": "", "body_numerals": []}
    assert checks.ungrounded_numerals([story]) == []


def test_a_letter_glued_to_digits_is_not_a_figure() -> None:
    assert checks.numeral_values("an H100 and GPT-6") == {"6"}


def test_the_probe_case_is_caught() -> None:
    """The fabricated figure of 2026-09-04, verbatim."""
    story = {
        "article_id": 72,
        "summary": "Enabled Intelligence da 500 binden fazla saatlik görüntüyü hazırladı.",
        "why_it_matters": "",
        "body_numerals": ["100", "2010"],
    }
    assert checks.ungrounded_numerals([story]) == [(72, ["500"])]


# -- the other pure functions -------------------------------------------------


def test_unrepresented_fives_sees_through_a_cluster() -> None:
    """A five whose *other outlet* made the digest is represented."""
    stories = [
        {
            "article_id": 1,
            "title": "Nvidia is buying Hugging Face for $13 billion",
            "importance": 5,
            "position": None,
        },
        {
            "article_id": 2,
            "title": "Nvidia is buying Hugging Face for $13 billion - The Verge",
            "importance": 4,
            "position": 1,
        },
        {
            "article_id": 3,
            "title": "A lab ships a model nobody ranked",
            "importance": 5,
            "position": None,
        },
    ]
    assert checks.unrepresented_fives(stories) == [3]


def test_fallback_order_is_importance_then_weight() -> None:
    stories = [
        {"article_id": 1, "importance": 3, "weight": 2.0},
        {"article_id": 2, "importance": 4, "weight": 0.5},
        {"article_id": 3, "importance": 3, "weight": 1.0},
    ]
    assert checks.fallback_order(stories, 3) == [2, 1, 3]


def test_editor_note_shape_counts_paragraphs_and_words() -> None:
    note = "one two three\n\nfour five\n\n\nsix"
    assert checks.editor_note_shape(note) == {"paragraphs": 3, "words": [3, 2, 1]}
    assert checks.editor_note_shape(None) == {"paragraphs": 0, "words": []}


# -- the editor's placements (ADR 0030) ---------------------------------------


def test_tier_shape_counts_the_bands_and_the_editors_disagreements() -> None:
    """A bulletin that is fifteen `notable` is a ranker filling a page. And a
    lower-importance story placed above a higher one is the editor reading
    against the free ordering, which is what an editor is for - all of it would
    mean the two are not reading the same thing."""
    stories = [
        {"article_id": 1, "importance": 3, "position": 1, "tier": "lead"},
        {"article_id": 2, "importance": 5, "position": 2, "tier": "major"},
        {"article_id": 3, "importance": 4, "position": 3, "tier": "major"},
        {"article_id": 4, "importance": 5, "position": None, "tier": None},
    ]
    shape = checks.tier_shape(stories)
    assert shape["n_ranked"] == 3
    assert shape["counts"] == {"lead": 1, "major": 2, "notable": 0, "brief": 0}
    # (1, 2) and (1, 3): a 3 published above a 5 and above a 4.
    assert shape["contradictions"] == 2
    assert shape["contradiction_share"] == pytest.approx(2 / 3)
    assert checks.tier_shape([])["n_ranked"] == 0


def test_the_tag_vocabulary_share_reads_the_prompts_own_list() -> None:
    from ainews.pipeline.prompts import TAG_VOCABULARY

    stories = [{"tags": ["openai", "agents", "my-own-words"]}, {"tags": ["openai"]}]
    vocabulary = checks.tag_vocabulary(stories)
    assert "openai" in TAG_VOCABULARY and "my-own-words" not in TAG_VOCABULARY
    assert vocabulary["in_vocabulary_share"] == pytest.approx(3 / 4)


@pytest.mark.parametrize("fixture", _fixtures())
def test_the_vocabulary_share_is_reported_not_asserted(fixture: dict[str, Any]) -> None:
    """Both fixtures predate the list; the number says how the model's own
    words overlapped it, which is the baseline the next recording is read
    against."""
    share = checks.tag_vocabulary(fixture["stories"])["in_vocabulary_share"]
    assert 0.0 <= share <= 1.0
