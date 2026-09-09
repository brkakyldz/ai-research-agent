"""Three readings of a day, and the one bulletin they become.

The aggregation is arithmetic over lists of ids, so it is asked directly rather
than through a rendered page: what Borda does with a story two readings kept and
one dropped, what the tier vote does with a split, whether the agreement number
can be high when the three readings disagree about *membership* rather than
about order.

The rank call itself is faked. What is under test is what production does with
three answers, not whether the model gives good ones - that is
`ainews eval rank-stability`, and it costs money.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import Settings
from ainews.db import Article, Bulletin, BulletinItem, Source, Summary
from ainews.pipeline.agreement import agreement, borda, jaccard, kendall_tau
from ainews.pipeline.nodes import rank as rank_module
from ainews.pipeline.nodes.rank import (
    Candidate,
    _aggregate,
    _majority_tier,
    _Pass,
    build_candidate_table,
    build_previous_block,
    day_pool,
    previous_headlines,
    rank_day,
)

# -- the measures -------------------------------------------------------------


def test_two_identical_orders_agree_completely() -> None:
    assert kendall_tau([1, 2, 3], [1, 2, 3]) == 1.0
    assert jaccard([1, 2, 3], [1, 2, 3]) == 1.0


def test_a_reversed_order_is_total_disagreement() -> None:
    assert kendall_tau([1, 2, 3], [3, 2, 1]) == -1.0


def test_one_shared_story_is_no_agreement_rather_than_perfect() -> None:
    """The gate is on tau, so a convention of 1.0 here would make it pass
    hardest exactly where the two readings share least - two lists that overlap
    in one story have not been shown to agree about anything."""
    assert kendall_tau([1, 2, 3], [3, 8, 9]) == 0.0


def test_agreement_is_the_weaker_of_the_two_measures() -> None:
    """They fail independently and either failure disqualifies. Three readings
    can agree perfectly on five stories while disagreeing about which five
    belong; a mean of the two would let the order hide the membership."""
    orders = [[1, 2, 3], [1, 2, 3, 4, 5, 6]]
    assert kendall_tau(*orders) == 1.0
    assert agreement(orders) == pytest.approx(0.5)


def test_one_reading_agrees_with_nothing_and_says_so() -> None:
    assert agreement([[1, 2, 3]]) == 1.0


# -- the aggregate ------------------------------------------------------------


def test_a_story_two_of_three_readings_kept_is_published() -> None:
    assert borda([[1, 2], [1, 2], [3, 4]]) == [1, 2]


def test_a_story_only_one_reading_kept_is_not() -> None:
    """Publishing it because it scored well in its one appearance is the failure
    the quorum exists to prevent: the ranker does not agree it is news."""
    assert 9 not in borda([[1, 2, 9], [1, 2], [2, 1]])


def test_a_longer_list_is_itself_part_of_what_a_reading_said() -> None:
    """Second of twelve outscores first of three: how many stories a reading
    kept is a claim about the day, not a formatting choice."""
    assert borda([[7, 5], [5, 7]])[0] == 5


def test_the_aggregate_is_a_function_of_its_inputs_and_not_of_dict_order() -> None:
    orders = [[4, 1, 2], [1, 4, 2], [2, 4, 1]]
    assert borda(orders) == borda(list(reversed(orders)))


def test_a_split_tier_vote_falls_to_the_weaker_tier() -> None:
    """Two readings that split between lead and major have not agreed that this
    leads the day, and the page's only ranking indicator is size."""
    assert _majority_tier(["lead", "major"]) == "major"
    assert _majority_tier(["lead", "lead", "major"]) == "lead"
    assert _majority_tier(["notable", "brief", "brief"]) == "brief"


def _pass(order: list[int], tiers: dict[int, str], note: str = "n") -> _Pass:
    return _Pass(
        order=order,
        tiers=tiers,  # type: ignore[arg-type]
        reasons=dict.fromkeys(order, "because"),
        editor_note=note,
        tokens_in=10,
        tokens_out=5,
    )


def test_only_one_story_leads_however_many_readings_named_a_lead() -> None:
    """Three readings each allowed one lead can still name three stories, and
    the aggregate has to choose - the one it put first."""
    passes = [
        _pass([1, 2, 3], {1: "lead", 2: "major", 3: "notable"}),
        _pass([1, 3, 2], {1: "lead", 3: "lead", 2: "major"}),
        _pass([2, 1, 3], {2: "lead", 1: "lead", 3: "notable"}),
    ]
    ranking = _aggregate(passes, top_n=15)

    leads = [sid for sid in ranking.order if ranking.tiers[sid] == "lead"]
    assert leads == [ranking.order[0]]


def test_the_editors_note_comes_from_the_reading_nearest_the_published_order() -> None:
    """The prose in front of the reader has to describe the list the reader is
    looking at."""
    passes = [
        _pass([1, 2, 3], {1: "lead", 2: "major", 3: "notable"}, note="about one two three"),
        _pass([1, 2, 3], {1: "lead", 2: "major", 3: "notable"}, note="also one two three"),
        _pass([8, 9], {8: "lead", 9: "major"}, note="about eight and nine"),
    ]
    ranking = _aggregate(passes, top_n=15)

    assert ranking.order == [1, 2, 3]
    assert "eight" not in ranking.editor_note


def test_a_bulletin_from_one_surviving_call_carries_no_agreement_number() -> None:
    """`None` and not 1.0: one reading agrees with nothing, and a number
    invented for that case would look like a measured one."""
    ranking = _aggregate([_pass([1, 2], {1: "lead", 2: "major"})], top_n=15)
    assert ranking.agreement is None


# -- what the ranker is shown -------------------------------------------------


def _candidate(summary_id: int, **kwargs: object) -> Candidate:
    fields: dict[str, object] = {
        "summary_id": summary_id,
        "article_id": summary_id,
        "source": "OpenAI",
        "weight": 2.0,
        "title": f"Baslik {summary_id}",
        "summary": "Ozet.",
        "why_it_matters": "Onemi.",
        "importance": 4,
        "kind": "news",
        "age_hours": 5.0,
    }
    fields.update(kwargs)
    return Candidate(**fields)  # type: ignore[arg-type]


def test_the_table_carries_what_the_ranker_is_asked_to_judge_on() -> None:
    """The age, because a day's bulletin drawn from a seven-day window otherwise
    cannot tell this morning's release from last Tuesday's; and the
    why-it-matters, because that sentence is the summariser's own answer to the
    question the ranker is being asked."""
    table = build_candidate_table([_candidate(1, kind="roundup", age_hours=50.0)])
    assert "1." in table
    assert "roundup" in table
    assert "2d old" in table
    assert "Why it matters: Onemi." in table


def test_a_day_with_no_previous_bulletin_shows_no_empty_heading() -> None:
    """A prompt that announces a section and shows nothing invites the model to
    fill it in."""
    assert build_previous_block([]) == ""
    assert "PREVIOUSLY PUBLISHED" in build_previous_block(["Bir haber"])


# -- the pool -----------------------------------------------------------------


async def _summarised(
    session: AsyncSession,
    n: int,
    *,
    language: str = "tr",
    relevant: bool = True,
) -> Summary:
    source = Source(name=f"S{n}", url=f"https://s{n}.dev/feed", weight=1.5)
    session.add(source)
    await session.flush()
    article = Article(
        source_id=source.id,
        title=f"Story {n}",
        url=f"https://s{n}.dev/{n}",
        url_canonical=f"https://s{n}.dev/{n}",
        body_text="A body.",
    )
    session.add(article)
    await session.flush()
    summary = Summary(
        article_id=article.id,
        language=language,
        title_local=f"Baslik {n}",
        summary="Ozet.",
        why_it_matters="Onemi.",
        importance=3,
        relevant=relevant,
    )
    session.add(summary)
    await session.flush()
    return summary


async def test_the_pool_is_the_day_and_not_the_press(
    session: AsyncSession, settings: Settings
) -> None:
    """The whole of F1 in one assertion: a second press sees everything already
    summarised, not only what it just bought."""
    first = await _summarised(session, 1)
    second = await _summarised(session, 2)
    await session.commit()

    pool = await day_pool(session, "2026-09-09", "tr", settings)

    assert {c.summary_id for c in pool} == {first.id, second.id}


async def test_a_story_an_earlier_day_published_is_not_offered_again(
    session: AsyncSession, settings: Settings
) -> None:
    yesterday = await _summarised(session, 1)
    today = await _summarised(session, 2)
    bulletin = Bulletin(day="2026-09-08", language="tr", version=1, est_cost_usd=0.0)
    session.add(bulletin)
    await session.flush()
    session.add(
        BulletinItem(bulletin_id=bulletin.id, summary_id=yesterday.id, position=1, tier="lead")
    )
    await session.commit()

    pool = await day_pool(session, "2026-09-09", "tr", settings)

    assert [c.summary_id for c in pool] == [today.id]


async def test_todays_own_earlier_version_does_not_shrink_the_pool(
    session: AsyncSession, settings: Settings
) -> None:
    """Re-ranking today is the point. A story in version 1 has to be a candidate
    for version 2, or the second press publishes the day minus its lead."""
    story = await _summarised(session, 1)
    bulletin = Bulletin(day="2026-09-09", language="tr", version=1, est_cost_usd=0.0)
    session.add(bulletin)
    await session.flush()
    session.add(BulletinItem(bulletin_id=bulletin.id, summary_id=story.id, position=1, tier="lead"))
    await session.commit()

    pool = await day_pool(session, "2026-09-09", "tr", settings)

    assert [c.summary_id for c in pool] == [story.id]


async def test_an_irrelevant_summary_is_never_a_candidate(
    session: AsyncSession, settings: Settings
) -> None:
    """The source list was the only thing deciding whether an item was AI news,
    and one weight-1.5 source is a personal blog that also carries map
    projections."""
    await _summarised(session, 1, relevant=False)
    keep = await _summarised(session, 2)
    await session.commit()

    pool = await day_pool(session, "2026-09-09", "tr", settings)

    assert [c.summary_id for c in pool] == [keep.id]


async def test_the_other_languages_bulletin_is_a_different_day(
    session: AsyncSession, settings: Settings
) -> None:
    await _summarised(session, 1, language="en")
    turkish = await _summarised(session, 2, language="tr")
    await session.commit()

    pool = await day_pool(session, "2026-09-09", "tr", settings)

    assert [c.summary_id for c in pool] == [turkish.id]


async def test_the_previous_headlines_come_from_the_last_day_before_this_one(
    session: AsyncSession,
) -> None:
    story = await _summarised(session, 1)
    older = Bulletin(day="2026-09-07", language="tr", version=1, est_cost_usd=0.0)
    newer = Bulletin(day="2026-09-08", language="tr", version=1, est_cost_usd=0.0)
    session.add_all([older, newer])
    await session.flush()
    session.add(BulletinItem(bulletin_id=newer.id, summary_id=story.id, position=1, tier="lead"))
    await session.commit()

    assert await previous_headlines(session, "2026-09-09", "tr") == ["Baslik 1"]
    # Nothing before the first bulletin, and no empty block to explain.
    assert await previous_headlines(session, "2026-09-01", "tr") == []


# -- the three calls ----------------------------------------------------------


async def test_every_call_failing_still_ships_a_bulletin(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordered by importance and then source weight, which is what a person
    would do with the same table - and `agreement is None` beside it is what
    says no editor stood behind it."""

    async def _fails(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(rank_module, "rank_once", _fails)

    ranking = await rank_day(
        [_candidate(1, importance=2), _candidate(2, importance=5)], "tr", settings=settings
    )

    assert ranking.order == [2, 1]
    assert ranking.agreement is None
    assert ranking.tiers[2] == "lead"


async def test_the_first_arrangement_is_the_pool_and_the_rest_are_shuffled(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run is reproducible from its id, and the production reading is one of
    the shuffles rather than a privileged call beside them."""
    seen: list[list[int]] = []

    async def _record(candidates: list[Candidate], *_: object, **__: object) -> _Pass:
        seen.append([c.summary_id for c in candidates])
        order = [c.summary_id for c in candidates][:2]
        return _pass(order, dict.fromkeys(order, "major"))

    monkeypatch.setattr(rank_module, "rank_once", _record)

    pool = [_candidate(n) for n in range(1, 9)]
    await rank_day(pool, "tr", settings=settings, seed="abc")

    assert len(seen) == 3
    assert seen[0] == [c.summary_id for c in pool]
    assert seen[1] != seen[0]
