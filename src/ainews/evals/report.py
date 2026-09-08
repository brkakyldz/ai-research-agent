"""`ainews eval report`: every number in one place, dated, never edited.

Reads what the other commands wrote - the runs, the reader's verdicts, the
judge's rows, the rank probes - runs the deterministic checks over the live
database, and appends one dated section to `docs/evals.md`. A section is what
was measured on that date; a later run appends, it does not rewrite. Every
number names the function that produced it, so a reader who doubts one can go
and read it.

Nothing here spends money. The judge and the probe are separate commands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import PROJECT_ROOT, Settings, get_settings
from ainews.db import EvalResult, Run, Summary, Verdict
from ainews.db.models import utcnow
from ainews.evals import checks
from ainews.evals.judge import Calibration, calibrate
from ainews.evals.record import record_run

DEFAULT_PATH = PROJECT_ROOT / "docs" / "evals.md"
HEADER = """# Evaluation record

One dated section per `ainews eval report` invocation, appended and never
edited: a section is what was measured on that date. The functions named beside
each number live in `src/ainews/evals/`; the bounds the test suite holds them to
are in `tests/test_evals_checks.py`. What the numbers are for, and what they
trigger, is `docs/PLAN-EVALS.md` (E4, E5) and ADR 0019.
"""


def parse_since(text: str) -> int:
    """`30d` -> 30. Days are the only unit; a report is read by a person."""
    match = re.fullmatch(r"\s*(\d+)\s*d?\s*", text)
    if not match:
        raise ValueError(f"--since wants a number of days like '30d', not {text!r}")
    return int(match.group(1))


@dataclass(slots=True)
class RunReport:
    run_id: str
    language: str
    started_at: str
    n_stories: int
    n_ranked: int
    product_cost: float
    budget: dict[str, Any]
    ungrounded: list[tuple[int, list[str]]]
    tags: dict[str, Any]
    importance: dict[int, float]
    overlap: dict[str, Any]
    shift: dict[str, Any]
    unrepresented: list[int]
    editor_note: dict[str, Any]
    verdicts: dict[str, int]
    judge: dict[str, Any] | None
    stability: dict[str, Any] | None


@dataclass(slots=True)
class Report:
    since_days: int
    generated_at: str
    runs: list[RunReport] = field(default_factory=list)
    calibration: Calibration | None = None
    n_labels: int = 0
    product_cost: float = 0.0
    eval_cost: float = 0.0


async def _verdict_counts(session: AsyncSession, run_id: str, n_stories: int) -> dict[str, int]:
    rows = (
        await session.execute(
            select(Verdict.verdict, func.count())
            .join(Summary, Summary.id == Verdict.summary_id)
            .where(Summary.run_id == run_id)
            .group_by(Verdict.verdict)
        )
    ).all()
    counts = {"ok": 0, "wrong": 0}
    for verdict, n in rows:
        counts[verdict] = int(n)
    counts["unlabelled"] = max(n_stories - counts["ok"] - counts["wrong"], 0)
    return counts


async def _judge_summary(session: AsyncSession, run_id: str) -> dict[str, Any] | None:
    rows = list(
        (
            await session.execute(
                select(EvalResult)
                .where(EvalResult.run_id == run_id)
                .where(EvalResult.kind == "grounding")
            )
        ).scalars()
    )
    if not rows:
        return None
    # One row per judged summary, the latest if a summary was judged twice.
    latest: dict[int, EvalResult] = {}
    for row in sorted(rows, key=lambda r: (r.created_at, r.id)):
        latest[row.summary_id or 0] = row
    judged = list(latest.values())
    return {
        "n": len(judged),
        "passed": sum(1 for r in judged if r.passed is True),
        "failed": sum(1 for r in judged if r.passed is False),
        "unparsed": sum(1 for r in judged if r.passed is None),
        "claims": [r.detail for r in judged if r.passed is False and r.detail],
        "model": judged[0].model,
        "cost": sum(r.est_cost_usd for r in rows),
    }


async def _stability_summary(session: AsyncSession, run_id: str) -> dict[str, Any] | None:
    import json

    row = (
        await session.execute(
            select(EvalResult)
            .where(EvalResult.run_id == run_id)
            .where(EvalResult.kind == "rank_stability")
            .order_by(EvalResult.created_at.desc(), EvalResult.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    detail = json.loads(row.detail or "{}")
    return {
        "tau": detail.get("tau"),
        "jaccard": detail.get("jaccard"),
        "times": detail.get("times"),
        "n_fallback": detail.get("n_fallback", 0),
        "cost": row.est_cost_usd,
    }


async def _calibration(session: AsyncSession) -> tuple[Calibration | None, int]:
    """The 2x2 from rows already on record: the latest grounding row of every
    summary that carries a verdict. No call is made here."""
    rows = (
        await session.execute(
            select(Verdict.verdict, EvalResult.passed, EvalResult.created_at, EvalResult.summary_id)
            .join(EvalResult, EvalResult.summary_id == Verdict.summary_id)
            .where(EvalResult.kind == "grounding")
            .order_by(EvalResult.summary_id, EvalResult.created_at)
        )
    ).all()
    n_labels = int((await session.execute(select(func.count()).select_from(Verdict))).scalar_one())
    if not rows:
        return None, n_labels
    latest: dict[int, tuple[str, bool | None]] = {}
    for verdict, passed, _, summary_id in rows:
        latest[summary_id] = (verdict, passed)
    return calibrate(list(latest.values())), n_labels


async def build_report(
    session: AsyncSession,
    since_days: int = 30,
    settings: Settings | None = None,
    run_id: str | None = None,
) -> Report:
    """Every measured number for the runs in the window, or for one run.

    `run_id` narrows the per-run sections to one run; the window still bounds
    the spend totals and the calibration reads every label there is. It exists
    because the record was growing by the *report* count rather than the run
    count: three sections in `docs/evals.md` carried the 2026-09-04 run three
    times, its deterministic rows identical in all three.
    """
    settings = settings or get_settings()
    horizon = utcnow() - timedelta(days=since_days)
    report = Report(since_days=since_days, generated_at=utcnow().strftime("%Y-%m-%d %H:%M UTC"))

    query = (
        select(Run)
        .where(Run.kind != "collect")
        .where(Run.n_summarized > 0)
        .where(Run.started_at >= horizon)
        .order_by(Run.started_at.desc())
    )
    if run_id is not None:
        query = query.where(Run.id == run_id)
    runs = list((await session.execute(query)).scalars())
    for run in runs:
        fixture = await record_run(session, run.id, settings)
        stories = fixture["stories"]
        report.runs.append(
            RunReport(
                run_id=run.id,
                language=run.language,
                started_at=run.started_at.strftime("%Y-%m-%d"),
                n_stories=len(stories),
                n_ranked=len(checks.ranked_order(stories)),
                product_cost=run.est_cost_usd,
                budget=checks.word_budget(stories),
                ungrounded=checks.ungrounded_numerals(stories),
                tags=checks.tag_vocabulary(stories),
                importance=checks.importance_distribution(stories),
                overlap=checks.ranker_vs_fallback(stories),
                shift=checks.editor_shift(stories),
                unrepresented=checks.unrepresented_fives(stories),
                editor_note=checks.editor_note_shape(run.editor_note),
                verdicts=await _verdict_counts(session, run.id, len(stories)),
                judge=await _judge_summary(session, run.id),
                stability=await _stability_summary(session, run.id),
            )
        )

    report.calibration, report.n_labels = await _calibration(session)
    report.product_cost = sum(r.product_cost for r in report.runs)
    report.eval_cost = float(
        (
            await session.execute(
                select(func.coalesce(func.sum(EvalResult.est_cost_usd), 0.0)).where(
                    EvalResult.created_at >= horizon
                )
            )
        ).scalar_one()
    )
    return report


# -- rendering ----------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def render_run(run: RunReport) -> list[str]:
    budget = run.budget
    lines = [
        f"### Run `{run.run_id[:8]}` · {run.language} · {run.started_at} · "
        f"{run.n_stories} stories, {run.n_ranked} ranked",
        "",
        "| Measure | Value | Function |",
        "|---|---|---|",
        f"| Summaries over 55 words | {_pct(budget['over_share']['summary'])} "
        f"({budget['over_count']['summary']}/{run.n_stories}) | `checks.word_budget` |",
        f"| Why-it-matters over 20 words | {budget['over_count']['why_it_matters']} "
        f"| `checks.word_budget` |",
        f"| Titles over 10 words | {budget['over_count']['title_local']} | `checks.word_budget` |",
        f"| Summary sentence histogram | {budget['sentences']} | `checks.word_budget` |",
        f"| Ungrounded numerals | {len(run.ungrounded)} story(ies): "
        f"{', '.join(f'{aid} {nums}' for aid, nums in run.ungrounded) or 'none'} "
        f"| `checks.ungrounded_numerals` |",
        f"| Tag singleton share | {_pct(run.tags['singleton_share'])} of {run.tags['distinct']} "
        f"| `checks.tag_vocabulary` |",
        f"| Tags from the preferred vocabulary | {_pct(run.tags['in_vocabulary_share'])} of uses "
        f"| `checks.tag_vocabulary` |",
        f"| Importance 1..5 | {' / '.join(_pct(run.importance[s]) for s in range(1, 6))} "
        f"| `checks.importance_distribution` |",
        f"| Ranker vs fallback overlap | {run.overlap['overlap']} of {run.overlap['top_n']} "
        f"| `checks.ranker_vs_fallback` |",
        f"| Editor's corrections | {run.shift['n_changed']} of {run.shift['n_ranked']} ranked "
        f"({run.shift['up']} up, {run.shift['down']} down), mean shift "
        f"{run.shift['mean_abs_shift']:.2f} | `checks.editor_shift` |",
        f"| Unrepresented fives | {run.unrepresented or 'none'} | `checks.unrepresented_fives` |",
        f"| Editor's note | {run.editor_note['paragraphs']} paragraph(s), "
        f"{run.editor_note['words']} words | `checks.editor_note_shape` |",
        f"| Reader verdicts ok / wrong / unlabelled | {run.verdicts['ok']} / "
        f"{run.verdicts['wrong']} / {run.verdicts['unlabelled']} | `Verdict` rows |",
    ]
    if run.judge:
        j = run.judge
        rate = j["passed"] / (j["passed"] + j["failed"]) if (j["passed"] + j["failed"]) else None
        lines.append(
            f"| Judge pass rate on the sample | {_pct(rate)} ({j['passed']} passed, "
            f"{j['failed']} failed, {j['unparsed']} unparsed of {j['n']}, {j['model']}) "
            f"| `judge.judge_run` |"
        )
        for claim in j["claims"]:
            lines.append(f"| ↳ unsupported claim | {claim} | `judge.judge_one` |")
    else:
        lines.append("| Judge pass rate on the sample | not judged | `judge.judge_run` |")
    if run.stability:
        s = run.stability
        fallback = f", {s['n_fallback']} fell back" if s["n_fallback"] else ""
        lines.append(
            f"| Rank stability (tau / top-N Jaccard) | {s['tau']} / {s['jaccard']} "
            f"over {s['times']} shuffles{fallback} | `stability.rank_stability` |"
        )
    else:
        lines.append("| Rank stability | not probed | `stability.rank_stability` |")
    lines.append(f"| Product spend | ${run.product_cost:.4f} | `Run.est_cost_usd` |")
    lines.append("")
    return lines


def render_markdown(report: Report, existing: str = "") -> str:
    """The section, as it will be appended.

    `existing` is the record so far. A run whose block would be byte-identical
    to one already in it is written as one line pointing back rather than
    repeated: the record is immutable, but an immutable record that restates
    itself every time it is asked grows by the report count instead of the run
    count, and after a month nobody can find the section where a number moved.
    """
    lines = [
        f"## {report.generated_at} — last {report.since_days} days",
        "",
        f"Product spend **${report.product_cost:.4f}** across {len(report.runs)} run(s); "
        f"eval spend **${report.eval_cost:.4f}** (`Run.est_cost_usd`, `EvalResult.est_cost_usd`).",
        "",
    ]
    for run in report.runs:
        block = render_run(run)
        if existing and "\n".join(block[2:]) in existing:
            lines.extend([block[0], "", "Unchanged since an earlier section.", ""])
        else:
            lines.extend(block)
    if not report.runs:
        lines.extend(["No digest run in the window.", ""])

    lines.append("### Judge calibration")
    lines.append("")
    table = report.calibration
    if table is None:
        lines.append(
            f"No judged summary carries a reader's verdict yet ({report.n_labels} label(s) "
            "on record). Run `ainews eval judge --labelled` once there are about sixty."
        )
    else:
        lines.append(
            f"TPR (wrong caught) {_pct(table.tpr)} on {table.n_wrong} labelled wrong; "
            f"TNR (ok passed) {_pct(table.tnr)} on {table.n_ok} labelled ok; "
            f"precision {_pct(table.precision)} on {table.n_failed} judge failure(s) "
            f"(`judge.calibrate`)."
        )
        if not table.trusted:
            lines.append(
                "Not enough labels to trust TPR and TNR: thirty per class needed. "
                f"{report.n_labels} label(s) on record in total."
            )
    lines.append("")
    return "\n".join(lines) + "\n"


def read_record(path: Path | None = None) -> str:
    """The record as it stands, or the header a first report starts from."""
    path = path or DEFAULT_PATH
    return path.read_text(encoding="utf-8") if path.exists() else HEADER


def append_report(text: str, path: Path | None = None) -> Path:
    path = path or DEFAULT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_record(path)
    if not existing.endswith("\n"):
        existing += "\n"
    # This is the one file here that is never edited, so a rewrite that dies
    # halfway would take every earlier section with it. Write beside it and
    # rename: `replace` is atomic, so the record is either the old section
    # list or the new one, never a truncated prefix of either.
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(existing + "\n" + text, encoding="utf-8", newline="\n")
    tmp.replace(path)
    return path
