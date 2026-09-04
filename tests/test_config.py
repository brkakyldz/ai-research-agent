from __future__ import annotations

import pytest

from ainews.config import Settings, get_settings


def test_environment_overrides_reach_settings(settings: Settings) -> None:
    assert settings.openai_api_key == "sk-test-not-a-real-key"
    assert settings.llm_configured is True
    assert settings.tavily_configured is False


def test_template_placeholders_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key still holding the template's `xxxx` is not a configured key."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    get_settings.cache_clear()
    assert get_settings().llm_configured is False


def test_numeric_bounds_are_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEDUPE_SCORE_THRESHOLD", "150")
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        get_settings()


def test_sqlite_path_resolves_relative_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./data/app.db")
    get_settings.cache_clear()
    assert get_settings().sqlite_path.name == "app.db"
