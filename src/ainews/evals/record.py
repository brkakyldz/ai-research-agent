"""Turn a run into a fixture: `ainews eval record --run <id>`.

The fixture *is* the behaviour, the same way `prompts/*.md` are: re-recording is
a reviewed diff, and the checks in `tests/test_evals_checks.py` assert bounds
over it offline. The JSON is written deterministically - sorted keys, no
timestamp of its own - so recording the same run twice is byte-for-byte the same
file, and a diff means the database changed.

The article body is **not** stored. It is someone's copyrighted text and the
repository is public; the checks only need the body's numerals and capitalised
tokens, so that is what is recorded (and `test_the_fixture_carries_no_article_bodies`
holds the line).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.config import PROJECT_ROOT, Settings, get_settings
from ainews.db import Article, Run, Source, Summary
from ainews.evals.checks import numeral_values
from ainews.pipeline.nodes.summarize import MAX_BODY_CHARS

SCHEMA = 1
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "runs"

# A capitalised token: a word starting with an uppercase letter in either
# language. Enough for a future name-grounding check; capped so a long body does
# not swell the fixture (200 per story made the first one 170 KB).
_CAPITALISED = re.compile(r"\b[A-ZÇĞİÖŞÜ][\w\-']{1,}", re.UNICODE)
MAX_CAPITALISED = 80


async def resolve_run_id(session: AsyncSession, ref: str) -> str:
    """`latest`, a full id, or an unambiguous prefix of one."""
    if ref == "latest":
        run = (
            await session.execute(
                select(Run)
                .where(Run.kind != "collect")
                .where(Run.n_summarized > 0)
                .order_by(Run.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if run is None:
            raise LookupError("no digest run with summaries in the database")
        return run.id
    rows = list((await session.execute(select(Run.id).where(Run.id.like(f"{ref}%")))).scalars())
    if not rows:
        raise LookupError(f"no run starts with {ref!r}")
    if len(rows) > 1:
        raise LookupError(f"{ref!r} is ambiguous: {', '.join(rows)}")
    return rows[0]


def capitalised_tokens(body: str | None) -> list[str]:
    tokens = sorted({m.group(0) for m in _CAPITALISED.finditer(body or "")})
    return tokens[:MAX_CAPITALISED]


async def record_run(
    session: AsyncSession, run_id: str, settings: Settings | None = None
) -> dict[str, Any]:
    settings = settings or get_settings()
    run = await session.get(Run, run_id)
    if run is None:
        raise LookupError(f"no run {run_id}")

    rows = (
        await session.execute(
            select(Summary, Article, Source)
            .join(Article, Article.id == Summary.article_id)
            .join(Source, Source.id == Article.source_id)
            .where(Summary.run_id == run.id)
            .order_by(Summary.rank.asc().nullslast(), Summary.importance.desc(), Summary.id.asc())
        )
    ).all()

    stories: list[dict[str, Any]] = []
    for summary, article, source in rows:
        try:
            tags = json.loads(summary.tags_json or "[]")
        except json.JSONDecodeError:
            tags = []
        # The numerals are taken from the text the model was shown, not the
        # whole body: a figure past the prompt's cut-off is one the model could
        # not have read, so it counts as ungrounded, which is the point.
        seen = (article.body_text or "")[:MAX_BODY_CHARS]
        stories.append(
            {
                "article_id": article.id,
                "source": source.name,
                "weight": source.weight,
                "title": article.title,
                "title_local": summary.title_local,
                "summary": summary.summary,
                "why_it_matters": summary.why_it_matters,
                "tags": tags,
                "importance": summary.importance,
                "rank": summary.rank,
                "body_numerals": sorted(numeral_values(seen)),
                "body_capitalised": capitalised_tokens(seen),
            }
        )

    article_ids = {s["article_id"] for s in stories}
    dup_rows = (
        await session.execute(select(Article).where(Article.dup_of.in_(article_ids)))
    ).scalars()
    survivors = {a.id: a.title for _, a, _ in rows}
    dedupe_pairs = [
        {
            "article_id": dup.id,
            "dup_of": dup.dup_of,
            "title": dup.title,
            "dup_of_title": survivors.get(dup.dup_of),
        }
        for dup in sorted(dup_rows, key=lambda a: a.id)
    ]

    return {
        "schema": SCHEMA,
        "run_id": run.id,
        "language": run.language,
        "run_started_at": run.started_at.isoformat() if run.started_at else None,
        "run_finished_at": run.finished_at.isoformat() if run.finished_at else None,
        # The run row does not store which models wrote it; these are the knobs
        # at recording time, which is what was believed when the fixture was cut.
        "model_summarize": settings.openai_model_summarize,
        "model_rank": settings.openai_model,
        "top_n": settings.digest_top_n,
        "editor_note": run.editor_note,
        "stories": stories,
        "dedupe_pairs": dedupe_pairs,
    }


def fixture_name(fixture: dict[str, Any]) -> str:
    day = (fixture.get("run_started_at") or "undated")[:10]
    return f"{day}_{fixture['language']}.json"


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def dump_fixture(fixture: dict[str, Any]) -> str:
    """Valid JSON, one story per line.

    `indent=2` put every list element on its own line and made the first
    fixture 160 KB of mostly whitespace; one line per story keeps a re-record
    reviewable as a diff (a changed summary is one changed line) at a third of
    the size.
    """
    lines = ["{"]
    scalars = {k: v for k, v in fixture.items() if k not in ("stories", "dedupe_pairs")}
    for key in sorted(scalars):
        lines.append(f"  {_compact(key)}: {_compact(scalars[key])},")
    for key in ("dedupe_pairs", "stories"):
        items = fixture.get(key) or []
        lines.append(f"  {_compact(key)}: [")
        for i, item in enumerate(items):
            lines.append(f"    {_compact(item)}{',' if i < len(items) - 1 else ''}")
        lines.append("  ]," if key == "dedupe_pairs" else "  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def write_fixture(fixture: dict[str, Any], out_dir: Path | None = None) -> Path:
    out_dir = out_dir or FIXTURE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / fixture_name(fixture)
    path.write_text(dump_fixture(fixture), encoding="utf-8", newline="\n")
    return path


def load_fixture(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
