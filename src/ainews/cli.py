"""Command line entry point.

The dashboard is the interface, but a pipeline you cannot run from a terminal is
a pipeline you cannot debug. Everything here calls the same functions the
collect job and the dashboard's run button call - there is no second implementation.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ainews.config import get_settings
from ainews.db import get_engine, init_db
from ainews.db.session import dispose_engine, session_scope
from ainews.evals.cli import add_eval_parser, run_eval
from ainews.logging_conf import configure_logging
from ainews.observability import enable_tracing

# `pricing` and not `llm`: the model *names* are needed to build the parser, and
# `llm` costs 1.3 seconds of langchain that `ainews sources` has no use for.
from ainews.pipeline.pricing import MODEL_NAMES
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


async def _digest(
    language: str | None,
    model_summarize: str | None = None,
    model_rank: str | None = None,
) -> int:
    from ainews.pipeline.runner import run_digest

    settings = get_settings()
    if not settings.llm_configured:
        print("OPENAI_API_KEY is not set; a digest needs it.", file=sys.stderr)
        return 2
    await _prepare()
    run_id = await run_digest(
        language=language,  # type: ignore[arg-type]
        model_summarize=model_summarize,
        model_rank=model_rank,
    )
    print(f"digest run {run_id} finished")
    return 0


async def _prune(dry_run: bool) -> int:
    """Drop what nothing can reach: `pipeline/prune.py` says what that is."""
    from ainews.pipeline.prune import prune
    from ainews.pipeline.runner import RunBusy

    await _prepare()
    try:
        report = await prune(dry_run=dry_run)
    except RunBusy as exc:
        print(str(exc), file=sys.stderr)
        return 2
    verb = "would drop" if report.dry_run else "dropped"
    print(f"{verb} {report.articles} unsummarised article(s)")
    if not report.dry_run:
        print(f"reclaimed {report.total_bytes / 1024:.0f} KiB")
    print("summaries, bulletins, runs, verdicts and judge findings are never pruned")
    return 0


async def _resummarize(
    article_ids: list[int], unattributed: bool, model: str | None, dry_run: bool
) -> int:
    """Re-fetch a body and rewrite the summaries written from it.

    Spends money, so it says how much before it does and does nothing without
    ids. There is no confirmation prompt: unlike the press this is a terminal
    command with the article numbers typed into it, and the ids *are* the
    confirmation.
    """
    from ainews.pipeline.pricing import SUMMARIZE_TOKENS_PER_ARTICLE, estimate_cost, resolve_model
    from ainews.pipeline.repair import resummarize, source_of, unattributed_articles

    settings = get_settings()
    if not settings.llm_configured:
        print("OPENAI_API_KEY is not set; a rewrite needs it.", file=sys.stderr)
        return 2
    await _prepare()

    async with session_scope() as session:
        targets = list(article_ids)
        if unattributed:
            targets += [i for i in await unattributed_articles(session) if i not in targets]
        if not targets:
            print("nothing to do: pass article ids or --unattributed", file=sys.stderr)
            return 2

        chosen = resolve_model(model, settings.openai_model_summarize)
        tokens_in, tokens_out = SUMMARIZE_TOKENS_PER_ARTICLE
        estimate = estimate_cost(chosen, len(targets) * tokens_in, len(targets) * tokens_out)
        print(f"{len(targets)} article(s) on {chosen}, about ${estimate:.4f}")

        if dry_run:
            for article_id in targets:
                print(f"  {article_id}  {await source_of(session, article_id)}")
            return 0

        failed = 0
        for article_id in targets:
            result = await resummarize(session, article_id, settings, model=chosen)
            if result.ok:
                print(
                    f"  {article_id}: {result.summaries_rewritten} summary(ies) rewritten"
                    f" from a {result.body_source} body"
                )
            else:
                failed += 1
                print(f"  {article_id}: {result.error}", file=sys.stderr)
    return 1 if failed else 0


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


def _serve() -> int:
    """Run the dashboard.

    Host and port come from `Settings`, which reads `HOST` and `PORT` off the
    environment - so a launcher that hands the process a free port is obeyed,
    instead of a number baked into a command line that is wrong the moment
    something else is already listening on it.

    Not part of `dispatch`: `uvicorn.run` owns its own event loop, so it must
    not be started from inside one.
    """
    import uvicorn

    settings = get_settings()
    uvicorn.run("ainews.web.app:app", host=settings.host, port=settings.port, workers=1)
    return 0


def _utf8_stdio() -> None:
    """Print UTF-8 whatever the console's code page is.

    Windows hands a Python process the console's ANSI code page - cp1254 on a
    Turkish install - and `eval report` renders a table row that starts with an
    arrow no such page can encode. The report died on `print` before it reached
    `append_report`, so the numbers were computed, paid for, and thrown away.
    The file itself was never at fault: it is written UTF-8 either way.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:  # a captured or wrapped stream may not have it
            reconfigure(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    parser = argparse.ArgumentParser(prog="ainews", description="AI news digest agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("collect", help="poll every enabled feed once (no LLM, no cost)")

    digest = sub.add_parser("digest", help="run the full pipeline and write a digest")
    digest.add_argument("--language", choices=("tr", "en"), default=None)
    # The same two choices the confirmation on /runs offers (ADR 0020), so a
    # terminal run and a press can be made identical - which is what makes the
    # dashboard reproducible from a shell rather than merely similar.
    digest.add_argument(
        "--model-summarize",
        choices=MODEL_NAMES,
        default=None,
        help="model for the per-article summaries (default: OPENAI_MODEL_SUMMARIZE)",
    )
    digest.add_argument(
        "--model-rank",
        choices=MODEL_NAMES,
        default=None,
        help="model for the single ranking call (default: OPENAI_MODEL)",
    )

    sub.add_parser("sources", help="list the seeded feeds and their last status")
    pruning = sub.add_parser(
        "prune",
        help="drop unsummarised articles nothing can reach",
    )
    pruning.add_argument(
        "--dry-run",
        action="store_true",
        help="count what would go and delete nothing",
    )
    repairing = sub.add_parser(
        "resummarize",
        help="re-fetch an article's body and rewrite its summaries",
    )
    repairing.add_argument(
        "article_ids",
        nargs="*",
        type=int,
        metavar="ARTICLE_ID",
        help="articles to repair; omit and pass --unattributed instead",
    )
    repairing.add_argument(
        "--unattributed",
        action="store_true",
        help=(
            "every summarised article whose body provenance is unknown - the rows "
            "written before the article and the web context were separated"
        ),
    )
    repairing.add_argument(
        "--model",
        choices=MODEL_NAMES,
        default=None,
        help="model for the rewrite (default: OPENAI_MODEL_SUMMARIZE)",
    )
    repairing.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be repaired and spend nothing",
    )
    sub.add_parser("init", help="create the database and seed the feed list")
    sub.add_parser("serve", help="run the dashboard on HOST:PORT from the environment")
    add_eval_parser(sub)

    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level)
    enable_tracing()

    if args.command == "serve":
        return _serve()

    async def dispatch() -> int:
        try:
            if args.command == "collect":
                return await _collect()
            if args.command == "digest":
                return await _digest(args.language, args.model_summarize, args.model_rank)
            if args.command == "sources":
                return await _sources()
            if args.command == "prune":
                return await _prune(args.dry_run)
            if args.command == "resummarize":
                return await _resummarize(
                    args.article_ids, args.unattributed, args.model, args.dry_run
                )
            if args.command == "eval":
                return await run_eval(args)
            await _prepare()
            print(f"database ready at {get_settings().sqlite_path}")
            return 0
        finally:
            await dispose_engine()

    return asyncio.run(dispatch())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
