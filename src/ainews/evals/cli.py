"""The `ainews eval …` subcommands.

Kept out of `ainews/cli.py` so the product's entry point gains two lines, not a
second parser tree. `record` needs no key and no network; `judge` and
`rank-stability` spend money and say so before the first call; `report` reads
what the others wrote.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ainews.db import get_engine, init_db
from ainews.db.session import session_scope


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


async def _record(run_ref: str, out: Path | None) -> int:
    from ainews.evals.record import record_run, resolve_run_id, write_fixture

    await init_db(get_engine())
    async with session_scope() as session:
        run_id = await resolve_run_id(session, run_ref)
        fixture = await record_run(session, run_id)
    path = write_fixture(fixture, out)
    print(f"recorded run {run_id} ({len(fixture['stories'])} stories) to {path}")
    return 0


async def run_eval(args: argparse.Namespace) -> int:
    try:
        if args.eval_command == "record":
            return await _record(args.run, args.out)
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"unknown eval command {args.eval_command!r}", file=sys.stderr)
    return 2
