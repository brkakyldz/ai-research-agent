"""The sampled judge and the rank-stability probe (PLAN-EVALS E3.6), offline.

A fake model stands in for the judge and the ranker; nothing here has a key or
a network, and the one thing that would spend money - the call - is exactly
what the cost guard is tested to stop.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.clock import local_day
from ainews.config import Settings
from ainews.db import Article, Bulletin, BulletinItem, EvalResult, Run, Source, Summary, Verdict
from ainews.evals import judge as judge_module
from ainews.evals import stability
from ainews.evals.cli import run_eval
from ainews.evals.judge import (
    Calibration,
    CostGuard,
    GroundingVerdict,
    calibrate,
    choose_sample,
    format_calibration,
    judge_bulletin,
    judge_labelled,
)
from ainews.pipeline.agreement import jaccard, kendall_tau, mean_pairwise
from ainews.pipeline.nodes import rank as rank_module
from ainews.pipeline.state import Pick, RankedDigest


class FakeMessage:
    def __init__(self, tokens_in: int = 900, tokens_out: int = 30) -> None:
        self.usage_metadata = {"input_tokens": tokens_in, "output_tokens": tokens_out}
        self.response_metadata: dict[str, Any] = {}


class FakeStructured:
    def __init__(self, answer: Any, *, calls: list[str], raise_with: Exception | None = None):
        self._answer = answer
        self._calls = calls
        self._raise = raise_with

    async def ainvoke(self, prompt: str, *_: object, **__: object) -> dict[str, Any]:
        self._calls.append(prompt)
        if self._raise:
            raise self._raise
        answer = self._answer(prompt) if callable(self._answer) else self._answer
        return {"parsed": answer, "raw": FakeMessage()}


class FakeLLM:
    def __init__(self, answer: Any, *, raise_with: Exception | None = None) -> None:
        self.answer = answer
        self.calls: list[str] = []
        self.raise_with = raise_with

    def with_structured_output(self, _schema: Any, **__: object) -> FakeStructured:
        return FakeStructured(self.answer, calls=self.calls, raise_with=self.raise_with)


BODY = "Nvidia will pay $12.9 billion for Hugging Face, which hosts three million models. " * 8


@pytest.fixture
async def bulletin(session: AsyncSession) -> Bulletin:
    """A published bulletin: three stories on the page, three the day left out.

    Both halves, because `choose_sample` prefers the published ones and fills
    the rest from below the fold - a judge that only ever saw the bulletin would
    measure the ranker's taste as much as the summariser's grounding.
    """
    src = Source(name="Ars", url="https://ars.dev/feed", weight=1.2)
    session.add(src)
    await session.flush()
    run = Run(kind="digest", language="tr", status="ok", n_summarized=6)
    session.add(run)
    await session.flush()
    bulletin = Bulletin(
        day=local_day(), language="tr", version=1, est_cost_usd=0.004, run_id=run.id
    )
    session.add(bulletin)
    await session.flush()
    for i in range(6):
        art = Article(
            source_id=src.id,
            title=f"Story {i}",
            url=f"https://ars.dev/{i}",
            url_canonical=f"https://ars.dev/{i}",
            body_text=BODY,
        )
        session.add(art)
        await session.flush()
        summary = Summary(
            article_id=art.id,
            language="tr",
            title_local=f"Haber {i}",
            summary=f"Ozet {i}. Iki. Uc.",
            why_it_matters="Onemli.",
            tags_json=json.dumps(["nvidia"]),
            importance=3 + (i % 3),
        )
        session.add(summary)
        await session.flush()
        if i < 3:
            session.add(
                BulletinItem(
                    bulletin_id=bulletin.id,
                    summary_id=summary.id,
                    position=i + 1,
                    tier="lead" if i == 0 else "major",
                )
            )
    await session.commit()
    return bulletin


@pytest.fixture
def fake_judge(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    llm = FakeLLM(GroundingVerdict(passed=True))
    monkeypatch.setattr(judge_module, "judge_model", lambda *_: llm)
    return llm


# -- the judge ----------------------------------------------------------------


async def test_the_judge_refuses_to_run_without_a_key(
    env_free_of_keys: None, capsys: pytest.CaptureFixture[str]
) -> None:
    args = argparse.Namespace(
        eval_command="judge", run="latest", sample=12, seed=0, max_cost=0.1, labelled=False
    )
    assert await run_eval(args) == 2
    assert "OPENAI_API_KEY is not set" in capsys.readouterr().err


def test_the_sample_is_stable_under_a_seed() -> None:
    candidates = [judge_module.Candidate(i, "r", i, "s", "t", "b", "sum", "why") for i in range(50)]
    first = [c.summary_id for c in choose_sample(candidates, 12, seed=7)]
    second = [c.summary_id for c in choose_sample(list(reversed(candidates)), 12, seed=7)]
    other = [c.summary_id for c in choose_sample(candidates, 12, seed=8)]
    assert first == second, "input order must not change the sample"
    assert len(first) == 12 and first == sorted(first)
    assert first != other
    assert len(choose_sample(candidates[:5], 12, seed=0)) == 5, "a small run is judged whole"


async def test_the_cost_guard_stops_before_the_first_call(
    session: AsyncSession, bulletin: Bulletin, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLM(GroundingVerdict(passed=True), raise_with=AssertionError("must not be called"))
    monkeypatch.setattr(judge_module, "judge_model", lambda *_: llm)

    with pytest.raises(CostGuard) as exc:
        await judge_bulletin(session, bulletin.id, sample=6, max_cost=0.0, settings=settings)
    assert "over --max-cost" in str(exc.value)
    assert llm.calls == []
    assert (await session.execute(select(EvalResult))).scalars().all() == []


async def test_a_judged_run_writes_one_row_per_summary_with_its_cost(
    session: AsyncSession, bulletin: Bulletin, settings: Settings, fake_judge: FakeLLM
) -> None:
    report = await judge_bulletin(
        session, bulletin.id, sample=4, seed=1, max_cost=1.0, settings=settings
    )
    assert (report.n_passed, report.n_failed, report.n_unparsed) == (4, 0, 0)
    assert len(fake_judge.calls) == 4
    assert BODY[:30] in fake_judge.calls[0], "the judge reads the body the summariser read"

    rows = (await session.execute(select(EvalResult))).scalars().all()
    assert len(rows) == 4
    assert {r.kind for r in rows} == {"grounding"}
    assert all(r.passed is True and r.model == settings.openai_model_judge for r in rows)
    assert all(r.est_cost_usd > 0 and r.tokens_in == 900 for r in rows)
    assert report.est_cost_usd == pytest.approx(sum(r.est_cost_usd for r in rows))


async def test_a_failed_claim_is_recorded_verbatim(
    session: AsyncSession, bulletin: Bulletin, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def answer(prompt: str) -> GroundingVerdict:
        if "Ozet 2." in prompt:
            return GroundingVerdict(passed=False, unsupported_claim="500 bin veri kümesi")
        return GroundingVerdict(passed=True)

    monkeypatch.setattr(judge_module, "judge_model", lambda *_: FakeLLM(answer))
    report = await judge_bulletin(session, bulletin.id, sample=6, max_cost=1.0, settings=settings)
    assert report.n_failed == 1
    failed = (
        await session.execute(select(EvalResult).where(EvalResult.passed.is_(False)))
    ).scalar_one()
    assert failed.detail == "500 bin veri kümesi"


async def test_a_judge_that_fails_to_parse_is_recorded_not_raised(
    session: AsyncSession, bulletin: Bulletin, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(judge_module, "judge_model", lambda *_: FakeLLM(None))
    report = await judge_bulletin(session, bulletin.id, sample=2, max_cost=1.0, settings=settings)
    assert report.n_unparsed == 2
    rows = (await session.execute(select(EvalResult))).scalars().all()
    assert [r.passed for r in rows] == [None, None]
    assert all(r.detail == "unparsable output" for r in rows)


async def test_an_upstream_error_is_a_row_not_a_traceback(
    session: AsyncSession, bulletin: Bulletin, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLM(None, raise_with=RuntimeError("simulated 500"))
    monkeypatch.setattr(judge_module, "judge_model", lambda *_: llm)
    report = await judge_bulletin(session, bulletin.id, sample=1, max_cost=1.0, settings=settings)
    assert report.outcomes[0].error is not None and "simulated 500" in report.outcomes[0].error
    row = (await session.execute(select(EvalResult))).scalar_one()
    assert row.passed is None and "simulated 500" in (row.detail or "")


# -- calibration --------------------------------------------------------------


def test_tpr_and_tnr_are_reported_separately() -> None:
    """Nine ok summaries the judge passes and one wrong one it misses is 90%
    accuracy and a judge that catches nothing. The table has to say so."""
    labels: list[tuple[str, str | None, bool | None]] = [("ok", None, True)] * 9 + [
        ("wrong", "wrong_fact", True)
    ]
    table = calibrate(labels)
    assert table.tnr == 1.0
    assert table.tpr == 0.0
    assert (table.n_ok, table.n_wrong) == (9, 1)
    text = format_calibration(table)
    assert "TPR" in text and "TNR" in text
    assert "accuracy" not in text.lower()
    assert "not enough labels to trust" in text


def test_a_trusted_table_needs_thirty_per_class() -> None:
    labels = [("ok", None, True)] * 30 + [("wrong", "wrong_fact", False)] * 29
    assert calibrate(labels).trusted is False
    labels.append(("wrong", "wrong_fact", False))
    assert calibrate(labels).trusted is True


def test_unparsed_judgements_are_counted_apart() -> None:
    table = calibrate([("ok", None, None), ("wrong", "wrong_fact", False)])
    assert table == Calibration(wrong_caught=1, unparsed=1)


def test_only_a_wrong_fact_is_scored_against_the_grounding_judge() -> None:
    """Four claims sit in one story block and this judge reads one of them.

    A reader marking a faithful summary of an irrelevant story used to count as
    a grounding miss, so the judge's TPR fell for being right. `not_news`,
    `duplicate` and `wrong_place` leave the table entirely - they are not "ok"
    either, because a story the reader rejected is no evidence the summary is
    faithful (PLAN-V2 5.1).
    """
    table = calibrate(
        [
            ("wrong", "wrong_fact", False),
            ("wrong", "not_news", True),
            ("wrong", "duplicate", True),
            ("wrong", "wrong_place", True),
            # A label from before the reader was asked which of the four.
            ("wrong", None, True),
            ("ok", None, True),
        ]
    )
    assert (table.wrong_caught, table.wrong_missed) == (1, 0)
    assert table.tpr == 1.0, "the judge is not punished for the three it never reads"
    assert table.other_reason == 4
    assert "set aside" in format_calibration(table)


async def test_labelled_mode_judges_only_what_the_reader_labelled(
    session: AsyncSession, bulletin: Bulletin, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = list((await session.execute(select(Summary.id).order_by(Summary.id))).scalars())
    session.add(Verdict(summary_id=ids[0], verdict="wrong", reason="wrong_fact", note="uydurma"))
    session.add(Verdict(summary_id=ids[1], verdict="ok"))
    await session.commit()

    def answer(prompt: str) -> GroundingVerdict:
        return GroundingVerdict(passed="Ozet 0." not in prompt, unsupported_claim=None)

    llm = FakeLLM(answer)
    monkeypatch.setattr(judge_module, "judge_model", lambda *_: llm)
    table, outcomes = await judge_labelled(session, max_cost=1.0, settings=settings)
    assert len(outcomes) == 2 and len(llm.calls) == 2
    assert (table.wrong_caught, table.ok_passed) == (1, 1)
    assert table.trusted is False


# -- rank stability -----------------------------------------------------------


async def test_the_probe_measures_the_shuffles_against_the_published_order(
    session: AsyncSession, bulletin: Bulletin, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ranker that always answers "1, 2, 3" against a shuffled table returns a
    different story order each time, and the probe has to notice - including how
    far each one is from the page the reader was actually given."""
    seen: list[str] = []

    class Ranker(FakeLLM):
        def with_structured_output(self, _schema: Any, **__: object) -> FakeStructured:
            return FakeStructured(
                RankedDigest(
                    editor_note="n",
                    picks=[Pick(number=n, tier="major", reason="") for n in (1, 2, 3)],
                ),
                calls=seen,
            )

    monkeypatch.setattr(rank_module, "ranker", lambda *_: Ranker(None))
    report = await stability.rank_stability(
        session, bulletin.id, times=3, seed=0, settings=settings
    )

    assert len(seen) == 3
    # Four orders, not three: the one that shipped is in the comparison, which
    # is the whole difference between this and the number production computes.
    assert len(report.orders) == 4
    assert report.orders[0] == await stability.published_order(session, bulletin.id)
    assert report.tau < 1.0 or report.jaccard < 1.0, "position-bound answers read as unstable"

    row = (await session.execute(select(EvalResult))).scalar_one()
    assert row.kind == "rank_stability" and row.summary_id is None
    assert row.bulletin_id == bulletin.id
    assert json.loads(row.detail or "{}")["times"] == 3


async def test_a_failed_call_is_counted_and_never_stands_in_for_an_answer(
    session: AsyncSession, bulletin: Bulletin, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed rank call contributes no order at all. An importance ordering
    invented here would be perfectly stable and would say nothing about the
    model, which is the number this command exists to produce."""
    monkeypatch.setattr(
        rank_module, "ranker", lambda *_: FakeLLM(None, raise_with=RuntimeError("down"))
    )
    report = await stability.rank_stability(session, bulletin.id, times=2, settings=settings)

    assert report.n_fallback == 2
    assert report.orders == [await stability.published_order(session, bulletin.id)]
    row = (await session.execute(select(EvalResult))).scalar_one()
    assert row.passed is None
    assert json.loads(row.detail or "{}")["n_fallback"] == 2


def test_three_disjoint_orders_do_not_pass_the_gate() -> None:
    """The failure the old gate could not see.

    `kendall_tau` returned 1.0 below two shared items, so three pairwise-disjoint
    top-5 orders scored tau 1.0 and passed a floor of 0.6 - the gate passed
    hardest exactly where the readings shared least. tau is 0.0 there now, and
    the floor reads `min(tau, corrected Jaccard)`, so both halves have to hold.
    """
    report = stability.StabilityReport(
        bulletin_id=1,
        times=3,
        pool=27,
        orders=[[1, 2, 3, 4, 5], [6, 7, 8, 9, 10], [11, 12, 13, 14, 15]],
    )
    report.tau = mean_pairwise(report.orders, kendall_tau)
    report.jaccard = mean_pairwise(report.orders, jaccard)

    assert report.tau == 0.0
    assert report.jaccard == 0.0
    assert report.passed is False


def test_the_gate_is_not_cleared_by_the_pool_being_small() -> None:
    """Fifteen of twenty-seven picked twice at random overlaps 0.385 of the time.

    Against a raw Jaccard that is two thirds of a 0.6 floor bought with nothing,
    and three readings agreeing only slightly better than coin flips would clear
    it. Corrected, chance scores zero.
    """
    kept = list(range(15))
    report = stability.StabilityReport(
        bulletin_id=1,
        times=2,
        pool=27,
        orders=[kept, kept[:9] + list(range(15, 21))],
    )
    report.tau = 1.0
    report.jaccard = mean_pairwise(report.orders, jaccard)

    assert report.jaccard == pytest.approx(0.4286, abs=0.0001), "above the 0.385 of chance"
    assert report.adjusted_jaccard == pytest.approx(0.0714, abs=0.0001)
    assert report.passed is False, "and nowhere near having earned a pass"


# -- the sample follows the reader (2026-09-08) --------------------------------


def test_the_sample_is_drawn_from_the_published_stories_first() -> None:
    """The reader labels what the page shows, which is the bulletin. A sample
    drawn uniformly over ninety summaries held one or two of them, so a
    judgement and a label almost never landed on the same story."""
    candidates = [
        judge_module.Candidate(
            i, 1, i, "s", "t", "b", "sum", "why", position=i + 1 if i < 3 else None
        )
        for i in range(20)
    ]
    chosen = choose_sample(candidates, 5, seed=0)
    assert [c.summary_id for c in chosen if c.position is not None] == [0, 1, 2], "every one"
    assert len(chosen) == 5, "the rest filled from below the fold"

    two = choose_sample(candidates, 2, seed=0)
    assert all(c.position is not None for c in two), "a small sample stays on the page"
    assert two == choose_sample(list(reversed(candidates)), 2, seed=0), "still seeded"


def test_precision_is_the_share_of_judge_failures_the_reader_agreed_with() -> None:
    """The number a one-reader tool acts on: when the judge raises its hand,
    is it right. Two failures the reader confirmed, one it did not: 67%."""
    table = calibrate(
        [
            ("wrong", "wrong_fact", False),
            ("wrong", "wrong_fact", False),
            ("ok", None, False),
            ("ok", None, True),
        ]
    )
    assert table.n_failed == 3
    assert table.precision == pytest.approx(2 / 3)
    assert "precision" in format_calibration(table)
    assert calibrate([("ok", None, True)]).precision is None, "no failures, no precision"
