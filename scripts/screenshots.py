"""Regenerate the README screenshots.

Kept as a script rather than done by hand so the images in the README can be
refreshed after a design change without anyone remembering what window size or
scroll position the last set used.

It starts the app itself against whatever is in `data/app.db`, so the pictures
show real summaries rather than fixtures.

    uv run python scripts/screenshots.py
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"

# Wide enough for the 47rem column plus air, tall enough that the digest page
# needs one scroll rather than five.
VIEWPORT = {"width": 1180, "height": 1000}
DEVICE_SCALE = 2  # readable when GitHub renders the PNG at half size

SHOTS: list[tuple[str, str, bool]] = [
    # path, filename, full_page
    ("/", "digest.png", False),
    ("/sources", "sources.png", False),
    ("/runs", "runs.png", False),
    ("/search?q=model", "search.png", False),
]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (URLError, OSError):
            time.sleep(0.3)
    raise SystemExit(f"the app never answered on {url}")


async def capture(base: str) -> None:
    from playwright.async_api import async_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport=VIEWPORT, device_scale_factor=DEVICE_SCALE)
        for path, name, full_page in SHOTS:
            await page.goto(f"{base}{path}", wait_until="networkidle")
            # The fonts are self-hosted, but they still load asynchronously and a
            # screenshot taken mid-swap shows the fallback stack.
            await page.evaluate("document.fonts.ready")
            await page.screenshot(path=str(OUT / name), full_page=full_page)
            print(f"wrote {OUT / name}")
        await browser.close()


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ)
    # No scheduler: a screenshot run must not trigger a real digest.
    env["SCHEDULER_ENABLED"] = "false"

    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "ainews.web.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=env,
    )
    try:
        wait_for(f"{base}/health")
        asyncio.run(capture(base))
    finally:
        server.terminate()
        server.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
