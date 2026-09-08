"""The same-origin guard on the four POSTs that do something.

There is no login here (README, "Known limits") and loopback is not a boundary a
browser respects: any page open in the same browser can post to
`http://localhost:8000/runs/start`. Two of these spend money, one writes the
evaluation labels, and one makes the server fetch a URL the sender chose.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

STATE_CHANGING = [
    ("/runs/start", {}),
    ("/runs/resume", {"run": "whatever"}),
    ("/verdict", {"summary_id": "1", "verdict": "ok"}),
    ("/sources/toggle", {"source_id": "1"}),
    ("/sources/add", {"url": "https://example.com/feed.xml"}),
]


@pytest.mark.parametrize("path,data", STATE_CHANGING)
def test_a_cross_site_post_is_refused(client: TestClient, path: str, data: dict) -> None:
    """What a stranger's page produces: the browser labels the request itself and
    the label cannot be forged by page script."""
    response = client.post(path, data=data, headers={"sec-fetch-site": "cross-site"})
    assert response.status_code == 403
    assert "cross-site" in response.text


@pytest.mark.parametrize("path,data", STATE_CHANGING)
def test_a_post_from_another_origin_is_refused(client: TestClient, path: str, data: dict) -> None:
    """The older half of the same fact, for a browser that sends no
    `Sec-Fetch-Site`: `Origin` naming a host this server is not."""
    response = client.post(path, data=data, headers={"origin": "https://evil.example"})
    assert response.status_code == 403


@pytest.mark.parametrize("path,data", STATE_CHANGING)
def test_the_app_s_own_pages_are_not_refused(client: TestClient, path: str, data: dict) -> None:
    """What htmx sends from this app's own pages. Whatever each route then
    answers - a refusal, a 404, a fragment - it is not the middleware's 403."""
    host = client.base_url.host
    response = client.post(
        path,
        data=data,
        headers={"sec-fetch-site": "same-origin", "origin": f"http://{host}"},
    )
    assert response.status_code != 403


def test_a_non_browser_caller_is_left_alone(client: TestClient) -> None:
    """curl and the health probe send neither header, and a caller with no
    ambient cookies is not the thing this defends against. Refusing them would
    close nothing and break the terminal."""
    assert client.post("/verdict", data={"summary_id": "1", "verdict": "ok"}).status_code != 403


def test_reading_a_page_is_never_refused(client: TestClient) -> None:
    """A GET changes nothing, so a link followed from anywhere still works."""
    assert client.get("/", headers={"sec-fetch-site": "cross-site"}).status_code == 200


def test_the_dashboard_binds_to_loopback_by_default() -> None:
    """`ainews serve` on a laptop used to answer every interface on the network."""
    from ainews.config import Settings

    assert Settings(_env_file=None).host == "127.0.0.1"
