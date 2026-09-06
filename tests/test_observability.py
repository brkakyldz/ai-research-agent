"""Tracing is opt-in, silent when it cannot start, and never fatal."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator

import pytest

from ainews import observability
from ainews.config import get_settings


@pytest.fixture(autouse=True)
def _forget_previous_instrumentation() -> Iterator[None]:
    """`enable_tracing` latches, like `configure_logging`."""
    observability._ENABLED = False
    yield
    observability._ENABLED = False


def test_tracing_is_off_unless_it_is_asked_for() -> None:
    assert get_settings().phoenix_enabled is False
    assert observability.enable_tracing() is False


def test_the_suite_never_loads_the_exporter() -> None:
    """What must stay unloaded is the Phoenix client and the OTLP exporter -
    they are what would open a socket. The OpenTelemetry SDK is not on this
    list: LangGraph imports it on its own once the `obs` group is installed,
    and with no exporter configured that is an import and nothing more."""
    observability.enable_tracing()
    assert "phoenix.otel" not in sys.modules
    assert not [m for m in sys.modules if m.startswith("opentelemetry.exporter")]


def test_a_missing_package_is_a_warning_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("PHOENIX_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(observability, "_load", lambda: None)

    with caplog.at_level(logging.WARNING):
        assert observability.enable_tracing() is False
    assert "uv sync --group obs" in caplog.text


def test_a_collector_that_is_not_there_does_not_stop_the_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A development instrument that can fail a digest is worse than none."""
    monkeypatch.setenv("PHOENIX_ENABLED", "true")
    get_settings.cache_clear()

    def _explode(**kwargs: object) -> None:
        raise RuntimeError("no collector here")

    monkeypatch.setattr(observability, "_load", lambda: (_explode, object))

    with caplog.at_level(logging.WARNING):
        assert observability.enable_tracing() is False
    assert "continuing without it" in caplog.text


def test_instrumenting_happens_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHOENIX_ENABLED", "true")
    get_settings.cache_clear()
    calls: list[str] = []

    class _Instrumentor:
        def instrument(self, **kwargs: object) -> None:
            calls.append("instrument")

    monkeypatch.setattr(observability, "_load", lambda: (lambda **kw: "provider", _Instrumentor))

    assert observability.enable_tracing() is True
    assert observability.enable_tracing() is True
    assert calls == ["instrument"]
