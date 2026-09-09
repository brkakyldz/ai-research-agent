"""Regenerate the README screenshots.

Kept as a script rather than done by hand so the images in the README can be
refreshed after a design change without anyone remembering what window size or
scroll position the last set used.

It starts the app itself against whatever is in `data/app.db`, so the pictures
show real summaries rather than fixtures.

    uv run python scripts/screenshots.py            # all of them
    uv run python scripts/screenshots.py runs.png   # just the one that changed

Naming files is what keeps a one-page change from rewriting the other seven: the
pictures are taken against live data, so a shot that did not need refreshing
still comes back a few pixels different and lands in the diff.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"

# Wide enough for the 246px rail plus a panel that is not squeezed, tall enough
# that the digest page needs one scroll rather than five.
WIDTH = 1320
HEIGHT = 1000
DEVICE_SCALE = 2  # readable when GitHub renders the PNG at half size


class Shot(NamedTuple):
    """One picture. The theme is a query parameter for the same reason the
    language is: it makes a screenshot reproducible without clicking first.

    `height` is the window, not a crop. A page shorter than the default leaves
    the rest of the shot as empty ground, which in a README reads as a bug in
    the layout rather than as a short page - so a short page gets a short
    window, and the rail's foot stays where it belongs, at the bottom of it.
    """

    path: str
    name: str
    full_page: bool = False
    height: int = HEIGHT


SHOTS: list[Shot] = [
    Shot("/?theme=dark", "digest.png"),
    Shot("/?theme=light", "digest-light.png"),
    Shot("/sources?theme=dark", "sources.png"),
    Shot("/runs?theme=dark", "runs.png"),
    # `{run}` is filled in with the newest bulletin's id: the page is about one
    # run, and hard-coding an id would make the script stop working the first
    # time the database it reads is not the one it was written against.
    # 960 is the six-node table plus the free checks under it: `main` is the
    # height of its content and the shell clips it at the window, so a window
    # shorter than the page cuts a block in half rather than scrolling it.
    Shot("/runs/{run}?theme=dark", "run-detail.png", height=960),
    Shot("/runs/verdicts?theme=dark", "verdicts.png"),
    Shot("/search?q=model&theme=dark", "search.png"),
    Shot("/archive?theme=light", "archive-light.png"),
]


def latest_digest_run() -> str:
    """The id `{run}` stands for: the newest run that actually wrote a bulletin.

    Read straight out of SQLite rather than off the `/runs` table, because the
    newest row there is usually a feed poll - which has one node, no model and
    no cost, and so takes a picture of an empty version of the page. The filter
    is `queries.digest_runs`, spelled in SQL.
    """
    from ainews.config import get_settings

    path = get_settings().sqlite_path
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        row = db.execute(
            "select id from runs where kind != 'collect' and n_summarized > 0 "
            "order by started_at desc limit 1"
        ).fetchone()
    if row is None:
        raise SystemExit(
            f"{path} holds no bulletin yet - run `uv run ainews digest` before taking "
            "a picture of one"
        )
    return str(row[0])


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


async def capture(base: str, wanted: list[str]) -> None:
    from playwright.async_api import async_playwright

    shots = [s for s in SHOTS if not wanted or s.name in wanted]
    run = latest_digest_run() if any("{run}" in s.path for s in shots) else ""
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=DEVICE_SCALE
        )
        for shot in shots:
            # Resized before the navigation, so a page whose layout answers to
            # the window height is measured at the height it is photographed at.
            await page.set_viewport_size({"width": WIDTH, "height": shot.height})
            await page.goto(f"{base}{shot.path.format(run=run)}", wait_until="networkidle")
            # The fonts are self-hosted, but they still load asynchronously and a
            # screenshot taken mid-swap shows the fallback stack.
            await page.evaluate("document.fonts.ready")
            await page.screenshot(path=str(OUT / shot.name), full_page=shot.full_page)
            print(f"wrote {OUT / shot.name}")
        await browser.close()


def main(argv: list[str] | None = None) -> int:
    wanted = list(argv if argv is not None else sys.argv[1:])
    known = {shot.name for shot in SHOTS}
    if unknown := [name for name in wanted if name not in known]:
        raise SystemExit(
            f"no such screenshot: {', '.join(unknown)}; known: {', '.join(sorted(known))}"
        )
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
        asyncio.run(capture(base, wanted))
    finally:
        server.terminate()
        server.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
