"""`ainews eval record` (PLAN-EVALS E1.2, E1.4): a bulletin becomes a fixture, offline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ainews.cli import main
from ainews.clock import local_day
from ainews.config import Settings
from ainews.db import Article, Bulletin, BulletinItem, Run, Source, Summary
from ainews.evals.cli import run_eval
from ainews.evals.record import dump_fixture, fixture_name, record_bulletin, write_fixture
from ainews.pipeline.runner import resolve_bulletin

BODY = (
    "Nvidia confirmed it will pay $12.9 billion for Hugging Face. The platform hosts "
    "three million models and 500,000 datasets, Jensen Huang said. " * 3
)


async def _seed_bulletin(session: AsyncSession, language: str = "tr") -> Bulletin:
    """One published story and one the editor left out - the fixture records
    both, because half of what `checks` measures is about the summariser."""
    src = Source(name="Ars Technica", url="https://ars.dev/feed", weight=1.2)
    session.add(src)
    await session.flush()
    run = Run(kind="digest", language=language, status="ok", n_summarized=2)
    session.add(run)
    await session.flush()
    bulletin = Bulletin(
        day=local_day(),
        language=language,
        version=1,
        editor_note="Gunun ozeti.",
        model_rank=None,
        est_cost_usd=0.004,
        run_id=run.id,
    )
    session.add(bulletin)
    await session.flush()
    for i, (title, position) in enumerate(
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
        summary = Summary(
            article_id=art.id,
            language=language,
            title_local=f"Başlık {i}",
            summary="Bir cümle. İki cümle. Üç cümle, 12,9 milyar dolar.",
            why_it_matters="Önemli.",
            tags_json=json.dumps(["nvidia", "funding"]),
            importance=4,
        )
        session.add(summary)
        await session.flush()
        if position is not None:
            session.add(
                BulletinItem(
                    bulletin_id=bulletin.id,
                    summary_id=summary.id,
                    position=position,
                    tier="lead",
                )
            )
    await session.commit()
    return bulletin


async def test_recording_the_same_bulletin_twice_is_byte_for_byte_identical(
    session: AsyncSession, settings: Settings
) -> None:
    bulletin = await _seed_bulletin(session)
    first = dump_fixture(await record_bulletin(session, bulletin.id, settings))
    second = dump_fixture(await record_bulletin(session, bulletin.id, settings))
    assert first == second
    assert json.loads(first)["bulletin_id"] == bulletin.id


async def test_the_fixture_carries_numerals_and_names_but_never_the_body(
    session: AsyncSession, settings: Settings
) -> None:
    bulletin = await _seed_bulletin(session)
    fixture = await record_bulletin(session, bulletin.id, settings)
    story = fixture["stories"][0]
    assert "12900000000" in story["body_numerals"]
    assert "500000" in story["body_numerals"]
    assert "Nvidia" in story["body_capitalised"]
    assert "body_text" not in story and "body" not in story
    assert BODY[:40] not in dump_fixture(fixture)


async def test_stories_come_in_reading_order(session: AsyncSession, settings: Settings) -> None:
    bulletin = await _seed_bulletin(session)
    fixture = await record_bulletin(session, bulletin.id, settings)
    assert [s["position"] for s in fixture["stories"]] == [1, None]
    assert [s["tier"] for s in fixture["stories"]] == ["lead", None]
    assert fixture["stories"][0]["source"] == "Ars Technica"
    assert fixture["stories"][0]["weight"] == 1.2


async def test_the_file_is_named_by_day_and_language(
    session: AsyncSession, settings: Settings, tmp_path: Path
) -> None:
    bulletin = await _seed_bulletin(session, language="en")
    fixture = await record_bulletin(session, bulletin.id, settings)
    assert fixture_name(fixture) == f"{bulletin.day}_en.json"
    path = write_fixture(fixture, tmp_path)
    assert path.parent == tmp_path
    assert json.loads(path.read_text(encoding="utf-8"))["language"] == "en"


async def test_a_second_version_of_a_day_gets_its_own_file(
    session: AsyncSession, settings: Settings
) -> None:
    """A day is what a person looks for, so the first version is just the day.
    A re-ranked day is a different fixture and must not overwrite it."""
    bulletin = await _seed_bulletin(session)
    first = fixture_name(await record_bulletin(session, bulletin.id, settings))
    bulletin.version = 2
    await session.commit()
    assert fixture_name(await record_bulletin(session, bulletin.id, settings)) != first


async def test_a_bulletin_is_found_by_day_or_latest(
    session: AsyncSession, settings: Settings
) -> None:
    bulletin = await _seed_bulletin(session)
    assert await resolve_bulletin(session, "latest") == bulletin.id
    assert await resolve_bulletin(session, bulletin.day) == bulletin.id
    assert await resolve_bulletin(session, f"{bulletin.day}:tr") == bulletin.id
    assert await resolve_bulletin(session, str(bulletin.id)) == bulletin.id
    with pytest.raises(LookupError):
        await resolve_bulletin(session, "1999-01-01")


async def test_the_cli_writes_the_fixture_without_a_key(
    session: AsyncSession,
    tmp_path: Path,
    env_free_of_keys: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bulletin = await _seed_bulletin(session)
    args = argparse.Namespace(eval_command="record", bulletin=str(bulletin.id), out=tmp_path)
    assert await run_eval(args) == 0
    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1
    assert "2 stories" in capsys.readouterr().out


async def test_an_unknown_bulletin_is_a_clean_exit_not_a_traceback(
    session: AsyncSession, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = argparse.Namespace(eval_command="record", bulletin="nope", out=tmp_path)
    assert await run_eval(args) == 2
    assert "no bulletin matches" in capsys.readouterr().err


def test_eval_is_a_subcommand_with_its_own_subcommands() -> None:
    """`ainews eval` alone is a usage error, the same way `ainews` alone is."""
    with pytest.raises(SystemExit) as exc:
        main(["eval"])
    assert exc.value.code == 2


async def test_the_fixture_names_the_models_that_ran_not_the_ones_configured(
    session: AsyncSession, settings: Settings
) -> None:
    """`run_steps` has said which model each paid node used since ADR 0022; a
    fixture cut under a changed `.env` must not name a model the run never saw."""
    from ainews.db import RunStep

    bulletin = await _seed_bulletin(session)
    session.add_all(
        [
            RunStep(run_id=bulletin.run_id, node="summarize", model="gpt-5.6-terra"),
            RunStep(run_id=bulletin.run_id, node="rank", model="gpt-5.6-sol"),
        ]
    )
    await session.commit()
    fixture = await record_bulletin(session, bulletin.id, settings)
    assert (fixture["model_summarize"], fixture["model_rank"]) == ("gpt-5.6-terra", "gpt-5.6-sol")


async def test_a_run_from_before_step_recording_falls_back_to_the_settings(
    session: AsyncSession, settings: Settings
) -> None:
    bulletin = await _seed_bulletin(session)
    fixture = await record_bulletin(session, bulletin.id, settings)
    assert fixture["model_summarize"] == settings.openai_model_summarize
