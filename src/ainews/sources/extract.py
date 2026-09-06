"""Turning whatever a feed gave us into text a model can summarise.

Feeds fall into three groups. Some ship the whole post in `content:encoded`,
which needs only tag-stripping. Some ship a two-line teaser, which is thin but
often enough. A linkblog or a newsletter roundup ships a sentence and someone
else's URL, which is nearly nothing at all.

So there are two functions: `clean_html` for the first two, and `fetch_article`
for the third. Both go through trafilatura, whose job is exactly this - pulling
the article out of a page full of navigation, cookie banners and related-story
rails - and both are synchronous C-and-Python, so callers run them in a thread.
"""

from __future__ import annotations

import logging
import re

import httpx
import trafilatura

from ainews.sources.rss import DEFAULT_HEADERS

log = logging.getLogger(__name__)

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")

# Enough for the model to work with; the rest is almost always related links.
MAX_BODY_CHARS = 6000
# Below this a "body" is a headline restated, and enrichment is worth trying.
MIN_USABLE_CHARS = 400


def _tidy(text: str) -> str:
    text = _SPACE.sub(" ", text)
    text = _BLANKS.sub("\n\n", text)
    return text.strip()[:MAX_BODY_CHARS]


def clean_html(raw: str | None) -> str:
    """Extract readable text from a feed's HTML body.

    trafilatura is tried first and a naive tag-strip is the fallback, because on
    a two-sentence teaser trafilatura sometimes decides there is no article at
    all and returns nothing - and a teaser is still better than an empty body.
    """
    if not raw:
        return ""
    stripped = raw.strip()
    if not stripped:
        return ""

    if "<" in stripped:
        try:
            extracted = trafilatura.extract(
                stripped,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )
        except Exception as exc:  # trafilatura raises on some malformed fragments
            log.debug("trafilatura failed on inline HTML: %s", exc)
            extracted = None
        if extracted:
            return _tidy(extracted)
        stripped = _TAG.sub(" ", stripped)

    return _tidy(stripped)


def is_usable(body: str | None) -> bool:
    return bool(body) and len(body.strip()) >= MIN_USABLE_CHARS


def fetch_article(url: str, timeout: float = 15.0) -> str:
    """Download `url` and extract its article text. Returns "" on any failure.

    Synchronous on purpose: trafilatura's own fetch helpers are, and the caller
    already has to move this off the event loop.
    """
    try:
        response = httpx.get(url, headers=DEFAULT_HEADERS, follow_redirects=True, timeout=timeout)
        response.raise_for_status()
    # `InvalidURL` is not an `HTTPError`: httpx raises it while building the
    # request, for a stored feed link like `http://[::1` or an un-encodable IDNA
    # host. Uncaught it escapes `asyncio.gather` in the enrich node and fails the
    # whole run over one bad link - the same trap `rss.fetch_feed` guards against.
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        log.debug("body fetch failed for %s: %s", url, exc)
        return ""

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type and "xml" not in content_type:
        return ""

    try:
        extracted = trafilatura.extract(
            response.text,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
            url=url,
        )
    except Exception as exc:
        log.debug("trafilatura failed on %s: %s", url, exc)
        return ""

    return _tidy(extracted or "")
