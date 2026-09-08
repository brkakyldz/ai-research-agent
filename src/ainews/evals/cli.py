"""The `ainews eval …` subcommands.

Kept out of `ainews/cli.py` so the product's entry point gains three lines, not
a second parser tree. `record` needs no key and no network; `judge` and
`rank-stability` spend money and say so before the first call; `report` reads
what the others wrote.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ainews.config import get_settings
from ainews.db import get_engine, init_db
from ainews.db.session import session_scope
from ainews.pipeline.pricing import MODEL_NAMES

NO_KEY = "OPENAI_API_KEY is not set; the judge needs it."


def add_eval_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = sub.add_parser("eval", help="measure the output (see docs/PLAN-EVALS.md)")
    evals = parser.add_subparsers(dest="eval_command", required=True)

    record = evals.add_parser("record", help="write a run's output to a JSON fixture (no key)")
    record.add_argument(
        "--run", default="latest", help="run id, an unambiguous prefix, or 'latest'"
    )
    record.add_argument(
        "--out", type=Path, default=None, help="directory (default: tests/fixtures/runs)"
    )

    judge = evals.add_parser(
        "judge", help="sampled grounding judge on one run (spends money; estimates first)"
    )
    judge.add_argument("--run", default="latest")
    judge.add_argument("--sample", type=int, default=12, help="summaries to judge (default 12)")
    judge.add_argument("--seed", type=int, default=0, help="same seed, same sample")
    judge.add_argument(
        "--max-cost", type=float, default=0.10, help="refuse above this estimate, USD"
    )
    # Named, not assumed (ADR 0020). The judge runs a tier above the pipeline,
    # which is ten times the price per token, so the one command most likely to
    # surprise a monthly bill is the one that should say out loud what it is
    # about to spend it on.
    judge.add_argument(
        "--model",
        choices=MODEL_NAMES,
        default=None,
        help="model to judge with (default: OPENAI_MODEL_JUDGE)",
    )
    judge.add_argument(
        "--labelled",
        action="store_true",
        help="calibration: judge every summary that has a reader's verdict, print TPR/TNR",
    )

    stability = evals.add_parser(
        "rank-stability", help="shuffle the rank table N ways and measure agreement"
    )
    stability.add_argument("--run", default="latest")
    stability.add_argument("--times", type=int, default=3)
    stability.add_argument("--seed", type=int, default=0)
    stability.add_argument(
        "--model",
        choices=MODEL_NAMES,
        default=None,
        help="model to probe (default: OPENAI_MODEL, the ranker)",
    )

    report = evals.add_parser(
        "report", help="print every number and append a dated section to docs/evals.md (no key)"
    )
    report.add_argument("--since", default="30d", help="window in days, e.g. 30d")
    report.add_argument(
        "--run",
        default=None,
        help="one run only (id, prefix or 'latest'); the spend totals keep the window",
    )
    report.add_argument("--out", type=Path, default=None, help="file (default: docs/evals.md)")
    report.add_argument("--no-write", action="store_true", help="print only")


async def _record(run_ref: str, out: Path | None) -> int:
    from ainews.evals.record import record_run, write_fixture
    from ainews.pipeline.runner import resolve_run_id

    await init_db(get_engine())
    async with session_scope() as session:
        run_id = await resolve_run_id(session, run_ref)
        fixture = await record_run(session, run_id)
    path = write_fixture(fixture, out)
    print(f"recorded run {run_id} ({len(fixture['stories'])} stories) to {path}")
    return 0


async def _judge(args: argparse.Namespace) -> int:
    from ainews.evals.judge import CostGuard, format_calibration, judge_labelled, judge_run
    from ainews.pipeline.pricing import resolve_model
    from ainews.pipeline.runner import resolve_run_id

    settings = get_settings()
    if not settings.llm_configured:
        print(NO_KEY, file=sys.stderr)
        return 2
    await init_db(get_engine())
    try:
        async with session_scope() as session:
            if args.labelled:
                table, outcomes = await judge_labelled(
                    session, max_cost=args.max_cost, model=args.model
                )
                model = resolve_model(args.model, settings.openai_model_judge)
                print(f"judged {len(outcomes)} labelled summaries on {model}")
                print(format_calibration(table))
                return 0
            run_id = await resolve_run_id(session, args.run)
            report = await judge_run(
                session,
                run_id,
                sample=args.sample,
                seed=args.seed,
                max_cost=args.max_cost,
                model=args.model,
            )
    except CostGuard as exc:
        print(str(exc), file=sys.stderr)
        return 3

    print(
        f"run {run_id}: {report.n_passed} passed, {report.n_failed} failed, "
        f"{report.n_unparsed} unparsed of {len(report.outcomes)} judged "
        f"on {report.model}, ~${report.est_cost_usd:.4f}"
    )
    for outcome in report.outcomes:
        if outcome.passed is False:
            print(f"  FAIL summary {outcome.candidate.summary_id}: {outcome.claim}")
        elif outcome.passed is None:
            print(f"  ???? summary {outcome.candidate.summary_id}: {outcome.error}")
    return 0


async def _stability(args: argparse.Namespace) -> int:
    from ainews.evals.stability import rank_stability
    from ainews.pipeline.runner import resolve_run_id

    settings = get_settings()
    if not settings.llm_configured:
        print(NO_KEY.replace("the judge", "the rank probe"), file=sys.stderr)
        return 2
    await init_db(get_engine())
    async with session_scope() as session:
        run_id = await resolve_run_id(session, args.run)
        report = await rank_stability(
            session, run_id, times=args.times, seed=args.seed, model=args.model
        )
    print(
        f"run {run_id}: {report.times} shuffled rank calls on {report.model}, "
        f"mean tau {report.tau:.3f}, mean top-N Jaccard {report.jaccard:.3f}, "
        f"~${report.est_cost_usd:.4f}"
        + (f", {report.n_fallback} fell back to importance order" if report.n_fallback else "")
    )
    return 0


async def _report(args: argparse.Namespace) -> int:
    from ainews.evals.report import (
        append_report,
        build_report,
        parse_since,
        read_record,
        render_markdown,
    )
    from ainews.pipeline.runner import resolve_run_id

    await init_db(get_engine())
    async with session_scope() as session:
        run_id = await resolve_run_id(session, args.run) if args.run else None
        report = await build_report(session, since_days=parse_since(args.since), run_id=run_id)
    # Rendered against the record it will be appended to, so a run already
    # written there in exactly these numbers is a pointer and not a repeat.
    text = render_markdown(report, existing="" if args.no_write else read_record(args.out))
    print(text, end="")
    if not args.no_write:
        path = append_report(text, args.out)
        print(f"appended to {path}", file=sys.stderr)
    return 0


async def run_eval(args: argparse.Namespace) -> int:
    try:
        if args.eval_command == "record":
            return await _record(args.run, args.out)
        if args.eval_command == "judge":
            return await _judge(args)
        if args.eval_command == "rank-stability":
            return await _stability(args)
        if args.eval_command == "report":
            return await _report(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"unknown eval command {args.eval_command!r}", file=sys.stderr)
    return 2
