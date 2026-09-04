"""Command line entry point.

The dashboard is the interface, but a pipeline you cannot run from a terminal is
a pipeline you cannot debug. Everything here calls the same functions the
scheduler and the "Run now" button call - there is no second implementation.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ainews.config import get_settings
from ainews.db import get_engine, init_db
from ainews.db.session import dispose_engine, session_scope
from ainews.logging_conf import configure_logging
from ainews.sources.seed import sync_sources


async def _prepare() -> None:
    await init_db(get_engine())
    async with session_scope() as session:
        await sync_sources(session)


async def _collect() -> int:
    from ainews.pipeline.runner import run_collect

    await _prepare()
    run_id = await run_collect()
    print(f"collect run {run_id} finished")
    return 0


async def _digest(language: str | None, mode: str) -> int:
    from ainews.pipeline.runner import run_digest

    settings = get_settings()
    if not settings.llm_configured:
        print("OPENAI_API_KEY is not set; a digest needs it.", file=sys.stderr)
        return 2
    await _prepare()
    run_id = await run_digest(language=language, mode=mode)  # type: ignore[arg-type]
    print(f"digest run {run_id} finished")
    return 0


async def _sources() -> int:
    from sqlalchemy import select

    from ainews.db import Source

    await _prepare()
    async with session_scope() as session:
        rows = (await session.execute(select(Source).order_by(Source.name))).scalars()
        for source in rows:
            # Checkbox reading: [x] is on. The inverse looked like every feed
            # was disabled.
            mark = "x" if source.enabled else " "
            print(f"[{mark}] {source.name:38} {source.last_status or '-'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ainews", description="AI news digest agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("collect", help="poll every enabled feed once (no LLM, no cost)")

    digest = sub.add_parser("digest", help="run the full pipeline and write a digest")
    digest.add_argument("--language", choices=("tr", "en"), default=None)
    digest.add_argument(
        "--mode",
        choices=("digest", "manual"),
        default="manual",
        help="'digest' is the scheduled 07:00 run; 'manual' is the same work, labelled",
    )

    sub.add_parser("sources", help="list the seeded feeds and their last status")
    sub.add_parser("init", help="create the database and seed the feed list")

    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)

    async def dispatch() -> int:
        try:
            if args.command == "collect":
                return await _collect()
            if args.command == "digest":
                return await _digest(args.language, args.mode)
            if args.command == "sources":
                return await _sources()
            await _prepare()
            print(f"database ready at {get_settings().sqlite_path}")
            return 0
        finally:
            await dispose_engine()

    return asyncio.run(dispatch())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
