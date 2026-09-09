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

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.clock import day_bounds
from ainews.config import PROJECT_ROOT, Settings, get_settings
from ainews.db import VERDICT_REASONS, Bulletin, BulletinItem, EvalResult, Summary, Verdict
from ainews.db.models import utcnow
from ainews.evals.judge import Calibration, calibrate
from ainews.evals.stability import FLOOR
from ainews.pipeline.prompts import prompt_version
from ainews.quality.stories import day_stories, published_checks

DEFAULT_PATH = PROJECT_ROOT / "docs" / "evals.md"
HEADER = """# Evaluation record

One dated section per `ainews eval report` invocation, appended and never
edited: a section is what was measured on that date. The functions named beside
each number live in `src/ainews/evals/`; the bounds the test suite holds them to
are in `tests/test_quality_checks.py`. What the numbers are for, and what they
trigger, is `docs/PLAN-EVALS.md` (E4, E5) and ADR 0019.
"""


def parse_since(text: str) -> int:
    """`30d` -> 30. Days are the only unit; a report is read by a person."""
    match = re.fullmatch(r"\s*(\d+)\s*d?\s*", text)
    if not match:
        raise ValueError(f"--since wants a number of days like '30d', not {text!r}")
    return int(match.group(1))


@dataclass(slots=True)
class BulletinReport:
    bulletin_id: int
    day: str
    version: int
    language: str
    agreement: float | None
    # Were the checks the press's own, or computed just now by today's code
    # over a day published before the column existed?
    at_publish: bool
    n_stories: int
    n_ranked: int
    product_cost: float
    budget: dict[str, Any]
    content: dict[str, Any]
    ungrounded: list[tuple[int, list[str]]]
    tags: dict[str, Any]
    importance: dict[int, float]
    overlap: dict[str, Any]
    tiers: dict[str, Any]
    unrepresented: list[int]
    editor_note: dict[str, Any]
    verdicts: dict[str, int]
    judge: dict[str, Any] | None
    stability: dict[str, Any] | None


@dataclass(slots=True)
class Report:
    since_days: int
    generated_at: str
    bulletins: list[BulletinReport] = field(default_factory=list)
    calibration: Calibration | None = None
    n_labels: int = 0
    # The prompt versions the calibration rows were measured under. More than
    # one means the rate averages two experiments.
    judge_prompts: list[str] = field(default_factory=list)
    product_cost: float = 0.0
    eval_cost: float = 0.0


def _stored_checks(bulletin: Bulletin) -> tuple[dict[str, Any] | None, bool]:
    """The checks the press wrote, or `(None, False)` if it wrote none.

    Preferred over recomputing because a number is a claim about the prompts and
    the check code that produced it, and both move. A bulletin published before
    the column existed is re-scored by today's code, and the section says so
    rather than passing it off as what was measured on the day.
    """
    if not bulletin.checks_json:
        return None, False
    try:
        return json.loads(bulletin.checks_json), True
    except json.JSONDecodeError:
        return None, False


async def _verdict_counts(
    session: AsyncSession, bulletin: Bulletin, n_stories: int
) -> dict[str, int]:
    """The reader's calls on the stories this bulletin's day produced.

    The day and not the bulletin: a verdict can be given on a story the editor
    left out - the page offers the two words on every rendered item - and those
    labels are exactly as useful to calibration as the published ones.
    """
    start, end = day_bounds(bulletin.day)
    in_bulletin = (
        select(BulletinItem.id)
        .where(BulletinItem.summary_id == Summary.id)
        .where(BulletinItem.bulletin_id == bulletin.id)
        .exists()
    )
    rows = (
        await session.execute(
            select(Verdict.verdict, Verdict.reason, func.count())
            .join(Summary, Summary.id == Verdict.summary_id)
            .where(Summary.language == bulletin.language)
            .where(in_bulletin | ((Summary.created_at >= start) & (Summary.created_at < end)))
            .group_by(Verdict.verdict, Verdict.reason)
        )
    ).all()
    # The four reasons are counted beside the two words. Only `wrong_fact` is
    # about the summariser; the other three are the relevance call, the dedupe
    # and the ranker, each of which has no other measurement at all.
    counts = {"ok": 0, "wrong": 0} | dict.fromkeys(VERDICT_REASONS, 0)
    for verdict, reason, n in rows:
        counts[verdict] = counts.get(verdict, 0) + int(n)
        if verdict == "wrong" and reason:
            counts[reason] = counts.get(reason, 0) + int(n)
    counts["unlabelled"] = max(n_stories - counts["ok"] - counts["wrong"], 0)
    return counts


async def _judge_summary(session: AsyncSession, bulletin_id: int) -> dict[str, Any] | None:
    rows = list(
        (
            await session.execute(
                select(EvalResult)
                .where(EvalResult.bulletin_id == bulletin_id)
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


async def _stability_summary(session: AsyncSession, bulletin_id: int) -> dict[str, Any] | None:

    row = (
        await session.execute(
            select(EvalResult)
            .where(EvalResult.bulletin_id == bulletin_id)
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
        # The number the gate reads: the weaker of tau and the Jaccard with this
        # pool's chance level taken out. Printed beside the raw pair rather than
        # instead of them, because "they shared 43% of the stories" and "that is
        # 7% better than picking at random" are both worth seeing.
        "score": detail.get("score"),
        "passed": row.passed,
        "times": detail.get("times"),
        "n_fallback": detail.get("n_fallback", 0),
        "cost": row.est_cost_usd,
    }


async def _calibration(session: AsyncSession) -> tuple[Calibration | None, int, list[str]]:
    """The 2x2 from rows already on record: the latest grounding row of every
    summary that carries a verdict. No call is made here.

    The prompt versions those rows came from are returned with it. A pass rate
    is a claim about a prompt, and averaging rows written by two of them is the
    mistake the 83-92% on record already made - so the number is reported with
    the prompts it was measured under named beside it.
    """
    rows = (
        await session.execute(
            select(
                Verdict.verdict,
                Verdict.reason,
                EvalResult.passed,
                EvalResult.created_at,
                EvalResult.summary_id,
                EvalResult.prompt_version,
            )
            .join(EvalResult, EvalResult.summary_id == Verdict.summary_id)
            .where(EvalResult.kind == "grounding")
            .order_by(EvalResult.summary_id, EvalResult.created_at)
        )
    ).all()
    n_labels = int((await session.execute(select(func.count()).select_from(Verdict))).scalar_one())
    if not rows:
        return None, n_labels, []
    latest: dict[int, tuple[str, str | None, bool | None]] = {}
    versions: dict[int, str] = {}
    for verdict, reason, passed, _, summary_id, version in rows:
        latest[summary_id] = (verdict, reason, passed)
        versions[summary_id] = version or "unknown"
    return calibrate(list(latest.values())), n_labels, sorted(set(versions.values()))


async def build_report(
    session: AsyncSession,
    since_days: int = 30,
    settings: Settings | None = None,
    bulletin_id: int | None = None,
) -> Report:
    """Every measured number for the bulletins in the window, or for one of them.

    `bulletin_id` narrows the per-bulletin sections to one; the window still
    bounds the spend totals and the calibration reads every label there is. It
    exists because the record was growing by the *report* count rather than the
    bulletin count: three sections in `docs/evals.md` carried the 2026-09-04 day
    three times, its deterministic rows identical in all three.
    """
    settings = settings or get_settings()
    horizon = utcnow() - timedelta(days=since_days)
    report = Report(since_days=since_days, generated_at=utcnow().strftime("%Y-%m-%d %H:%M UTC"))

    query = (
        select(Bulletin)
        .where(Bulletin.created_at >= horizon)
        .order_by(Bulletin.day.desc(), Bulletin.version.desc())
    )
    if bulletin_id is not None:
        query = query.where(Bulletin.id == bulletin_id)
    bulletins = list((await session.execute(query)).scalars())
    for bulletin in bulletins:
        stored, at_publish = _stored_checks(bulletin)
        if stored is None:
            stored = published_checks(await day_stories(session, bulletin), bulletin.editor_note)
        report.bulletins.append(
            BulletinReport(
                bulletin_id=bulletin.id,
                day=bulletin.day,
                version=bulletin.version,
                language=bulletin.language,
                agreement=bulletin.agreement,
                at_publish=at_publish,
                n_stories=stored["n"],
                n_ranked=stored["n_published"],
                # What the day cost: the ranking on the bulletin, plus every
                # summary it drew on. A summary carries its own spend now, so
                # this is a sum rather than a run total that included stories
                # published on another day.
                product_cost=bulletin.est_cost_usd + await _summary_cost(session, bulletin),
                budget=stored["budget"],
                content=stored["content"],
                ungrounded=[(row["article_id"], row["numerals"]) for row in stored["ungrounded"]],
                tags=stored["tags"],
                importance={int(k): v for k, v in stored["importance"].items()},
                overlap=stored["overlap"],
                tiers=stored["tiers"],
                unrepresented=stored["unrepresented"],
                editor_note=stored["editor_note"],
                verdicts=await _verdict_counts(session, bulletin, stored["n"]),
                judge=await _judge_summary(session, bulletin.id),
                stability=await _stability_summary(session, bulletin.id),
            )
        )

    report.calibration, report.n_labels, report.judge_prompts = await _calibration(session)
    report.product_cost = sum(b.product_cost for b in report.bulletins)
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


async def _summary_cost(session: AsyncSession, bulletin: Bulletin) -> float:
    """What the day's summaries cost, published or not."""
    start, end = day_bounds(bulletin.day)
    return float(
        (
            await session.execute(
                select(func.coalesce(func.sum(Summary.est_cost_usd), 0.0))
                .where(Summary.language == bulletin.language)
                .where(Summary.created_at >= start)
                .where(Summary.created_at < end)
            )
        ).scalar_one()
    )


# -- rendering ----------------------------------------------------------------


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def render_bulletin(run: BulletinReport) -> list[str]:
    budget = run.budget
    version = "" if run.version == 1 else f" v{run.version}"
    agreement = "n/a" if run.agreement is None else f"{run.agreement:.2f}"
    lines = [
        f"### Bulletin {run.day}{version} · {run.language} · "
        f"{run.n_ranked} published of {run.n_stories} summarised · agreement {agreement}",
        "",
        (
            "Checks as the press ran them."
            if run.at_publish
            # Said out loud, because otherwise a re-scored old day reads as a
            # measurement of that day, and it is a measurement of today's code.
            else "Checks recomputed now: this bulletin was published before the press "
            "stored its own, so these are today's checks over an older day."
        ),
        "",
        "| Measure | Value | Function |",
        "|---|---|---|",
        f"| Summaries over 55 words | {_pct(budget['over_share']['summary'])} "
        f"({budget['over_count']['summary']}/{run.n_stories}) | `checks.word_budget` |",
        f"| Why-it-matters over 20 words | {budget['over_count']['why_it_matters']} "
        f"| `checks.word_budget` |",
        f"| Titles over 10 words | {budget['over_count']['title_local']} | `checks.word_budget` |",
        f"| Summary sentence histogram | {budget['sentences']} | `checks.word_budget` |",
        f"| Key fact named | {_pct(run.content['key_fact_share'])} of {run.content['n']} "
        f"| `checks.content_floor` |",
        f"| ↳ and kept in the writing | {_pct(run.content['key_fact_kept'])} "
        f"| `checks.content_floor` |",
        f"| A body figure survives into the summary | {_pct(run.content['numeral_recall'])} "
        f"of {run.content['n_with_numerals']} | `checks.content_floor` |",
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
        f"| Tiers | {' / '.join(f'{t} {n}' for t, n in run.tiers['counts'].items())} "
        f"| `checks.tier_shape` |",
        f"| Editor against the free order | {run.tiers['contradictions']} inverted pair(s), "
        f"{_pct(run.tiers['contradiction_share'])} | `checks.tier_shape` |",
        f"| Unrepresented fives | {run.unrepresented or 'none'} | `checks.unrepresented_fives` |",
        f"| Editor's note | {run.editor_note['paragraphs']} paragraph(s), "
        f"{run.editor_note['words']} words | `checks.editor_note_shape` |",
        f"| Reader verdicts ok / wrong / unlabelled | {run.verdicts['ok']} / "
        f"{run.verdicts['wrong']} / {run.verdicts['unlabelled']} | `Verdict` rows |",
        "| ↳ wrong: fact / not news / duplicate / place | "
        f"{run.verdicts['wrong_fact']} / {run.verdicts['not_news']} / "
        f"{run.verdicts['duplicate']} / {run.verdicts['wrong_place']} | `Verdict.reason` |",
    ]
    if run.judge:
        j = run.judge
        rate = j["passed"] / (j["passed"] + j["failed"]) if (j["passed"] + j["failed"]) else None
        lines.append(
            f"| Judge pass rate on the sample | {_pct(rate)} ({j['passed']} passed, "
            f"{j['failed']} failed, {j['unparsed']} unparsed of {j['n']}, {j['model']}) "
            f"| `judge.judge_bulletin` |"
        )
        for claim in j["claims"]:
            lines.append(f"| ↳ unsupported claim | {claim} | `judge.judge_one` |")
    else:
        lines.append("| Judge pass rate on the sample | not judged | `judge.judge_bulletin` |")
    if run.stability:
        s = run.stability
        fallback = f", {s['n_fallback']} fell back" if s["n_fallback"] else ""
        gate = "not measured" if s["passed"] is None else ("pass" if s["passed"] else "FAIL")
        lines.append(
            f"| Rank stability (tau / top-N Jaccard) | {s['tau']} / {s['jaccard']} "
            f"over {s['times']} shuffles{fallback} | `stability.rank_stability` |"
        )
        lines.append(
            f"| ↳ against chance, gate {FLOOR} | {s['score']} — {gate} "
            "| `stability.StabilityReport.score` |"
        )
    else:
        lines.append("| Rank stability | not probed | `stability.rank_stability` |")
    lines.append(
        f"| Product spend | ${run.product_cost:.4f} "
        f"| `Bulletin.est_cost_usd` + `Summary.est_cost_usd` |"
    )
    lines.append("")
    return lines


def render_markdown(report: Report, existing: str = "") -> str:
    """The section, as it will be appended.

    `existing` is the record so far. A bulletin whose block would be
    byte-identical to one already in it is written as one line pointing back
    rather than repeated: the record is immutable, but an immutable record that
    restates itself every time it is asked grows by the report count instead of
    the bulletin count, and after a month nobody can find the section where a
    number moved.
    """
    lines = [
        f"## {report.generated_at} — last {report.since_days} days",
        "",
        f"Product spend **${report.product_cost:.4f}** across "
        f"{len(report.bulletins)} bulletin(s); eval spend **${report.eval_cost:.4f}** "
        "(`Bulletin.est_cost_usd`, `Summary.est_cost_usd`, `EvalResult.est_cost_usd`).",
        "",
    ]
    for bulletin in report.bulletins:
        block = render_bulletin(bulletin)
        if existing and "\n".join(block[2:]) in existing:
            lines.extend([block[0], "", "Unchanged since an earlier section.", ""])
        else:
            lines.extend(block)
    if not report.bulletins:
        lines.extend(["No bulletin in the window.", ""])

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
        prompts = report.judge_prompts
        current = prompt_version("judge_grounding", "en")
        if len(prompts) > 1:
            lines.append(
                "**These rows were judged under "
                f"{len(prompts)} different prompts** (`{'`, `'.join(prompts)}`), so the "
                "rate above averages two experiments. Re-judge the labelled set to "
                "read one number."
            )
        elif prompts and prompts != [current]:
            lines.append(
                f"Judged under prompt `{prompts[0]}`; the prompt on disk is now "
                f"`{current}`, so this measures the previous one."
            )
        elif prompts:
            lines.append(f"Judge prompt `{current}`.")
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
