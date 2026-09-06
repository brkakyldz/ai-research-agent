"""`ainews eval record` (PLAN-EVALS E1.2, E1.4): a run becomes a fixture, offline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.cli import main
from ainews.config import Settings
from ainews.db import Article, Run, Source, Summary
from ainews.evals.cli import run_eval
from ainews.evals.record import (
    dump_fixture,
    fixture_name,
    record_run,
    resolve_run_id,
    write_fixture,
)

BODY = (
    "Nvidia confirmed it will pay $12.9 billion for Hugging Face. The platform hosts "
    "three million models and 500,000 datasets, Jensen Huang said. " * 3
)


async def _seed_run(session: AsyncSession, language: str = "tr") -> Run:
    src = Source(name="Ars Technica", url="https://ars.dev/feed", weight=1.2)
    session.add(src)
    await session.flush()
    run = Run(kind="manual", language=language, status="ok", n_summarized=2)
    session.add(run)
    await session.flush()
    for i, (title, rank) in enumerate(
        [("Nvidia buys Hugging Face for $13 billion", 1), ("A quieter story", None)], start=1
    ):
        art = Article(
            source_id=src.id,
            title=title,
            url=f"https://ars.dev/{i}",
            url_canonical=f"https://ars.dev/{i}",
            body_text=BODY,
        )
        session.add(art)
        await session.flush()
        session.add(
            Summary(
                article_id=art.id,
                run_id=run.id,
                language=language,
                title_local=f"Başlık {i}",
                summary="Bir cümle. İki cümle. Üç cümle, 12,9 milyar dolar.",
                why_it_matters="Önemli.",
                tags_json=json.dumps(["nvidia", "funding"]),
                importance=4,
                rank=rank,
            )
        )
    await session.commit()
    return run


async def test_recording_the_same_run_twice_is_byte_for_byte_identical(
    session: AsyncSession, settings: Settings
) -> None:
    run = await _seed_run(session)
    first = dump_fixture(await record_run(session, run.id, settings))
    second = dump_fixture(await record_run(session, run.id, settings))
    assert first == second
    assert json.loads(first)["run_id"] == run.id


async def test_the_fixture_carries_numerals_and_names_but_never_the_body(
    session: AsyncSession, settings: Settings
) -> None:
    run = await _seed_run(session)
    fixture = await record_run(session, run.id, settings)
    story = fixture["stories"][0]
    assert "12900000000" in story["body_numerals"]
    assert "500000" in story["body_numerals"]
    assert "Nvidia" in story["body_capitalised"]
    assert "body_text" not in story and "body" not in story
    assert BODY[:40] not in dump_fixture(fixture)


async def test_stories_come_in_reading_order(session: AsyncSession, settings: Settings) -> None:
    run = await _seed_run(session)
    fixture = await record_run(session, run.id, settings)
    assert [s["rank"] for s in fixture["stories"]] == [1, None]
    assert fixture["stories"][0]["source"] == "Ars Technica"
    assert fixture["stories"][0]["weight"] == 1.2


async def test_the_file_is_named_by_run_date_and_language(
    session: AsyncSession, settings: Settings, tmp_path: Path
) -> None:
    run = await _seed_run(session, language="en")
    fixture = await record_run(session, run.id, settings)
    assert fixture_name(fixture) == f"{run.started_at.date().isoformat()}_en.json"
    path = write_fixture(fixture, tmp_path)
    assert path.parent == tmp_path
    assert json.loads(path.read_text(encoding="utf-8"))["language"] == "en"


async def test_a_run_is_found_by_prefix_or_latest(
    session: AsyncSession, settings: Settings
) -> None:
    run = await _seed_run(session)
    assert await resolve_run_id(session, run.id[:6]) == run.id
    assert await resolve_run_id(session, "latest") == run.id
    with pytest.raises(LookupError):
        await resolve_run_id(session, "zzzz")


async def test_the_cli_writes_the_fixture_without_a_key(
    session: AsyncSession,
    tmp_path: Path,
    env_free_of_keys: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = await _seed_run(session)
    args = argparse.Namespace(eval_command="record", run=run.id, out=tmp_path)
    assert await run_eval(args) == 0
    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1
    assert "2 stories" in capsys.readouterr().out


async def test_an_unknown_run_is_a_clean_exit_not_a_traceback(
    session: AsyncSession, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = argparse.Namespace(eval_command="record", run="nope", out=tmp_path)
    assert await run_eval(args) == 2
    assert "no run starts with" in capsys.readouterr().err


def test_eval_is_a_subcommand_with_its_own_subcommands() -> None:
    """`ainews eval` alone is a usage error, the same way `ainews` alone is."""
    with pytest.raises(SystemExit) as exc:
        main(["eval"])
    assert exc.value.code == 2
