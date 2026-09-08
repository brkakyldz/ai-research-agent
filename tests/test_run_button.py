"""The one press that spends money, and the module boundary around it.

`POST /runs/start` is the app's whole paid surface (ADR 0015), so what it refuses
and when is worth its own file rather than a section in the middle of a thousand
lines about page rendering.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from ainews.config import Settings, get_settings
from ainews.db import Run
from ainews.web.app import create_app

# -- run now ------------------------------------------------------------------


def test_run_now_refuses_without_a_key(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")
    get_settings.cache_clear()
    with TestClient(create_app(get_settings())) as fresh:
        body = fresh.post("/runs/start?lang=tr").text
    assert "OPENAI_API_KEY" in body


def test_run_now_starts_one_run_and_refuses_a_second(
    settings: Settings, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two presses must not become two runs competing for the same candidates.

    This one builds its own client inside a `with`, unlike the shared fixture.
    Without the context manager TestClient closes its event loop after every
    response; that destroys the pending digest task, which runs `_guarded`'s
    `finally` and releases the slot - so the second press would look free for a
    reason that has nothing to do with the application.
    """
    import asyncio

    from ainews.pipeline import runner

    started = 0

    async def _never_finishes(**_: object) -> str:
        """Stands in for a two-minute digest, so the slot is still held.

        It deliberately never returns. Sleeping for a fixed time instead would
        make the assertion a race: the runner releases the slot as soon as this
        returns, and the second request takes longer to arrive than any sleep
        short enough to keep the test fast.
        """
        nonlocal started
        started += 1
        await asyncio.Event().wait()
        return ""

    monkeypatch.setattr(runner, "run_digest", _never_finishes)

    with TestClient(create_app(settings)) as client:
        first = client.post("/runs/start?lang=tr")
        second = client.post("/runs/start?lang=tr")

    assert "Çalışıyor" in first.text
    assert "Zaten" in second.text
    assert started == 1


def test_the_template_environment_is_built_once(client: TestClient, digest: Run) -> None:
    """Twelve call sites built their own until 2026-09-08, so every render threw
    away the compile cache the previous one had filled."""
    from ainews.web.views import get_templates

    before = client.app.state.templates  # type: ignore[attr-defined]
    client.get("/")
    client.get("/runs")
    assert client.app.state.templates is before  # type: ignore[attr-defined]
    assert get_templates(SimpleNamespace(app=client.app)) is before  # type: ignore[arg-type]


def test_templates_reload_in_development_and_not_in_production(settings: Settings) -> None:
    """`ENVIRONMENT` promised this in the env template and only toggled the API
    docs; an environment built per request made it true by accident."""
    settings.environment = "development"
    assert create_app(settings).state.templates.env.auto_reload is True
    settings.environment = "production"
    assert create_app(settings).state.templates.env.auto_reload is False


async def test_two_simultaneous_presses_start_one_run(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard has to hold when both presses are genuinely in flight at once.

    `TestClient` cannot show this: its calls block, so the first task always gets
    to run before the second request is made. Only two coroutines awaited together
    reach the window between `create_task` scheduling the task and the task's own
    first line - the window a fire-and-forget run has to claim its slot before.
    """
    import asyncio

    from ainews.pipeline import runner
    from ainews.web.routes import runs as runs_route

    started: list[str] = []

    async def _fake_digest(
        language: str | None = None,
        mode: str | None = None,
        model_summarize: str | None = None,
        model_rank: str | None = None,
        **_: object,
    ) -> str:
        started.append(language or "")
        await asyncio.sleep(0.2)
        return "fake-run-id"

    monkeypatch.setattr(runner, "run_digest", _fake_digest)
    request = SimpleNamespace(cookies={}, query_params={})

    first, second = await asyncio.gather(
        runs_route.start_run(request, lang="tr", settings=settings),  # type: ignore[arg-type]
        runs_route.start_run(request, lang="tr", settings=settings),  # type: ignore[arg-type]
    )
    bodies = {first.body.decode(), second.body.decode()}

    await asyncio.sleep(0.4)
    assert started == ["tr"], "a second press must not become a second paid run"
    assert any("hx-get" in b for b in bodies), "one press must start the run"
    assert any("hx-get" not in b for b in bodies), "the other must be refused as busy"
    assert not runs_route.is_running(), "the slot must be released when the run ends"


# -- the module graph ----------------------------------------------------------


def test_the_web_layer_never_reaches_past_the_pipeline_api() -> None:
    """The three reach-ins finding 4 of the 2026-09-08 audit named.

    A function-level import is what a cycle looks like once it has been worked
    around rather than removed, so this asserts on the source: nothing under
    `web/` names a pipeline node or the runner, at the top of a file or inside
    one. `pipeline/api.py` is the whole surface.
    """
    import pathlib

    web = pathlib.Path(__file__).resolve().parents[1] / "src" / "ainews" / "web"
    offenders = []
    for path in web.rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "ainews.pipeline.nodes" in line and "import" in line:
                offenders.append(f"{path.name}:{n}")
            # `routes/runs.py` keeps one, as the seam a test patches `run_digest`
            # through; its docstring says so.
            if "ainews.pipeline.runner" in line and "import" in line and path.name != "runs.py":
                offenders.append(f"{path.name}:{n}")
    assert offenders == []


def test_queries_and_views_no_longer_import_each_other_inside_functions() -> None:
    """They did it four times, because the formatting half of `views` was on the
    wrong side of the line. It is `web/format.py` now and imports neither."""
    import pathlib

    web = pathlib.Path(__file__).resolve().parents[1] / "src" / "ainews" / "web"
    inline = [
        f"{p.name}:{n}"
        for p in (web / "queries.py", web / "views.py", web / "format.py")
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if line.startswith("    ") and " import " in line and "ainews" in line
    ]
    assert inline == []

    source = (web / "format.py").read_text(encoding="utf-8")
    assert "ainews.web.views" not in source
    assert "ainews.web.queries" not in source
