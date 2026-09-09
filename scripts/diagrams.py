"""Render the two diagrams in `docs/` from their HTML sources.

    uv sync --group docs && uv run playwright install chromium
    uv run python scripts/diagrams.py                    # both
    uv run python scripts/diagrams.py architecture.png   # just the one

The pictures in the README are drawings, and a drawing that lives only as a PNG
goes stale silently: nobody edits a picture to say that the checkpointer came
out. The source is `docs/diagrams/*.html`, so changing a box is an edit and a
diff like every other document here, and this script is the only thing that has
to know what width and device scale the last render used.

Nothing is served: the page is opened as a local file and it loads no asset the
repository does not carry.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "diagrams"
OUT = ROOT / "docs"

# The body is 1560px wide; at scale 2 the PNG is 3120 and stays readable when
# GitHub renders it at half size. Height is whatever the page turns out to be.
WIDTH = 1560
DEVICE_SCALE = 2

PAGES = ["architecture.html", "eval-architecture.html"]


async def render(wanted: list[str]) -> None:
    from playwright.async_api import async_playwright

    names = [p for p in PAGES if not wanted or p.replace(".html", ".png") in wanted]
    if not names:
        raise SystemExit(f"nothing to render; known: {', '.join(PAGES)}")

    async with async_playwright() as play:
        browser = await play.chromium.launch()
        page = await browser.new_page(
            viewport={"width": WIDTH, "height": 1200},
            device_scale_factor=DEVICE_SCALE,
        )
        for name in names:
            source = SRC / name
            await page.goto(source.as_uri())
            # The web fonts are the machine's own; without this the first render
            # can measure a fallback face and clip a box by a line.
            await page.wait_for_timeout(300)
            out = OUT / name.replace(".html", ".png")
            await page.screenshot(path=str(out), full_page=True)
            print(f"{out.relative_to(ROOT)}  {out.stat().st_size // 1024} KB")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(render(sys.argv[1:]))
