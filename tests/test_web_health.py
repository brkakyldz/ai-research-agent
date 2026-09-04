from __future__ import annotations

from fastapi.testclient import TestClient

from ainews.config import Settings
from ainews.web.app import create_app


def test_health_reports_state_without_leaking_keys(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        payload = client.get("/health").json()

    assert payload["status"] == "ok"
    assert payload["counts"] == {"sources": 0, "articles": 0, "runs": 0}
    assert payload["llm_configured"] is True
    assert payload["tavily_configured"] is False
    assert settings.openai_api_key not in repr(payload)


def test_index_renders(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "AI News Digest" in response.text
