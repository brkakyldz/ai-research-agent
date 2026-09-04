from __future__ import annotations

from fastapi.testclient import TestClient

from ainews.config import Settings
from ainews.web.app import create_app
from ainews.web.i18n import strings


def test_health_reports_state_without_leaking_keys(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        payload = client.get("/health").json()

    assert payload["status"] == "ok"
    # Starting the app seeds feeds.yaml, so a fresh install already has sources
    # and nothing else.
    assert payload["counts"]["sources"] >= 15
    assert payload["counts"]["articles"] == 0
    assert payload["counts"]["runs"] == 0
    assert payload["llm_configured"] is True
    assert payload["tavily_configured"] is False
    assert settings.openai_api_key not in repr(payload)


def test_index_renders(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/")
    assert response.status_code == 200
    # Assert the page came through the real template chain rather than pinning a
    # literal: this used to check the M0 placeholder's headline, which M4 removed.
    assert strings(settings.digest_language)["title"] in response.text
    assert "theme.css" in response.text
