"""Turn a bulletin into a fixture: `ainews eval record --bulletin <ref>`.

The fixture *is* the behaviour, the same way `prompts/*.md` are: re-recording is
a reviewed diff, and the checks in `tests/test_quality_checks.py` assert bounds
over it offline. The JSON is written deterministically - sorted keys, no
timestamp of its own - so recording the same bulletin twice is byte-for-byte the
same file, and a diff means the database changed.

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
from ainews.db import Article, Bulletin, Run, RunStep
from ainews.quality.stories import day_stories

__all__ = ["FIXTURE_DIR", "record_bulletin", "write_fixture"]

# Bumped when a story gains a field. 3 replaced `rank` and `editor_importance`
# with `position` and `tier`, and added `relevant` and `kind` (ADR 0030); 4 adds
# `key_fact`, which is what `checks.content_floor` reads.
SCHEMA = 4
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "runs"

# A capitalised token: a word starting with an uppercase letter in either
# language. Enough for a future name-grounding check; capped so a long body does
# not swell the fixture (200 per story made the first one 170 KB).
_CAPITALISED = re.compile(r"\b[A-ZÇĞİÖŞÜ][\w\-']{1,}", re.UNICODE)
MAX_CAPITALISED = 80


async def _models_that_ran(session: AsyncSession, run_id: str) -> dict[str, str]:
    """Which model each paid node actually used, off `run_steps` (ADR 0022).

    The fixture used to write the settings' two model knobs here, with a note
    that the run row does not store the model. It still does not, but the step
    rows have since ADR 0022, and a fixture cut a week after the run under a
    changed `.env` would otherwise name a model the run never saw.
    """
    rows = (
        await session.execute(
            select(RunStep.node, RunStep.model)
            .where(RunStep.run_id == run_id)
            .where(RunStep.model != "")
            .order_by(RunStep.started_at)
        )
    ).all()
    return dict(rows)


def capitalised_tokens(body: str | None) -> list[str]:
    tokens = sorted({m.group(0) for m in _CAPITALISED.finditer(body or "")})
    return tokens[:MAX_CAPITALISED]


async def record_bulletin(
    session: AsyncSession, bulletin_id: int, settings: Settings | None = None
) -> dict[str, Any]:
    """One published bulletin, and everything its day held, as a fixture.

    The bulletin and not the run (ADR 0030): what the checks measure is what was
    published, and a run is now a delta that may have bought two of the fifteen
    stories on the page. The unpublished rest of the day is recorded beside it,
    because half of what `checks` measures - the tag vocabulary, the importance
    distribution, the free ordering - is about the summariser rather than about
    the editor, and sampling only the winners would flatter it.
    """
    settings = settings or get_settings()
    bulletin = await session.get(Bulletin, bulletin_id)
    if bulletin is None:
        raise LookupError(f"no bulletin {bulletin_id}")
    run = await session.get(Run, bulletin.run_id) if bulletin.run_id else None

    stories = await day_stories(session, bulletin)

    article_ids = {story["article_id"] for story in stories}
    dup_rows = (
        await session.execute(select(Article).where(Article.dup_of.in_(article_ids)))
    ).scalars()
    survivors = {story["article_id"]: story["title"] for story in stories}
    dedupe_pairs = [
        {
            "article_id": dup.id,
            "dup_of": dup.dup_of,
            "title": dup.title,
            "dup_of_title": survivors.get(dup.dup_of),
        }
        for dup in sorted(dup_rows, key=lambda a: a.id)
    ]

    # A run recorded before ADR 0022 has no step rows; the settings' knobs are
    # then what was believed when the fixture was cut, as they always were.
    models = await _models_that_ran(session, run.id) if run else {}
    return {
        "schema": SCHEMA,
        "bulletin_id": bulletin.id,
        "day": bulletin.day,
        "version": bulletin.version,
        "language": bulletin.language,
        "agreement": bulletin.agreement,
        "published_at": bulletin.created_at.isoformat() if bulletin.created_at else None,
        "run_id": run.id if run else None,
        # The run row first, the steps for a run written before the columns
        # existed, and the environment last - which is the order of how much
        # each one knows about *this* bulletin. The settings' knob is the
        # weakest of the three and used to be second: a changed `.env` would
        # name a model the run never saw.
        "model_summarize": (
            (run.model_summarize if run else None)
            or models.get("summarize")
            or settings.openai_model_summarize
        ),
        "model_rank": bulletin.model_rank or models.get("rank") or settings.openai_model,
        "top_n": settings.digest_top_n,
        "editor_note": bulletin.editor_note,
        "stories": stories,
        "dedupe_pairs": dedupe_pairs,
    }


def fixture_name(fixture: dict[str, Any]) -> str:
    """`YYYY-MM-DD_tr.json`, with the version only where there is more than one.

    A day is what a person looks for, and the overwhelming majority of days
    have one bulletin. Naming the first `..._v1` would put a version number on
    every filename to disambiguate the rare second one.
    """
    day = fixture.get("day") or "undated"
    version = fixture.get("version") or 1
    tail = "" if version == 1 else f"_v{version}"
    return f"{day}_{fixture['language']}{tail}.json"


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
