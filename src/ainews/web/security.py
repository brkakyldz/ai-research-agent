"""The one thing standing between a stranger's web page and this dashboard.

There is no login here and there should not be: it is a personal tool on
loopback (ADR 0002, README "Known limits"). But loopback is not a boundary a
browser respects. Any page open in the same browser can `fetch` or submit a form
at `http://localhost:8000/runs/start` — the request goes out with the reader's
cookies, and the two POSTs that matter spend money (`/runs/start`,
`/runs/resume`), one writes evaluation labels (`/verdict`), and one makes this
server fetch a URL of the sender's choosing (`/sources/add`).

A token in every form would be the general answer. It is not the proportionate
one for a single-user app with no login to hang a session on: the whole attack
needs the request to *look* cross-site, and browsers say so themselves.

`Sec-Fetch-Site` is sent by every browser this app runs in, on every request, and
cannot be set by page script — a same-origin fetch says `same-origin`, a form
posted from another site says `cross-site`. `Origin` is the older half of the
same fact and browsers attach it to every POST. Either one disagreeing with this
server's own host is a refusal.

Neither header present means the caller is not a browser — curl, the test client,
a health probe — and a non-browser has no ambient cookies to ride, so it is not
the thing this defends against. Refusing those would break the terminal without
closing anything.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import PlainTextResponse
from starlette.responses import Response

log = logging.getLogger(__name__)

# GET and HEAD change nothing, so they are not the shape this guards.
GUARDED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# `same-origin` is this app's own pages. `none` is a typed address or a bookmark,
# which cannot carry a POST body from another site's page.
ALLOWED_SITE = frozenset({"same-origin", "none"})


def _same_host(origin: str, request: Request) -> bool:
    """Does `Origin` name the host this request arrived at?

    Compared against `Host` rather than against a configured URL: the app is
    reached as `localhost:8000` by its owner and as `127.0.0.1:8000` by the
    container's health check, and hard-coding either would refuse the other.
    Both are the same server, and both are what the browser will echo back.
    """
    sent = urlsplit(origin).netloc
    return bool(sent) and sent == request.headers.get("host", "")


async def same_origin_only(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    if request.method in GUARDED_METHODS:
        site = request.headers.get("sec-fetch-site")
        origin = request.headers.get("origin")
        if (site is not None and site not in ALLOWED_SITE) or (
            origin is not None and not _same_host(origin, request)
        ):
            log.warning(
                "refused a cross-site %s %s (origin=%r, sec-fetch-site=%r)",
                request.method,
                request.url.path,
                origin,
                site,
            )
            return PlainTextResponse("cross-site request refused", status_code=403)
    return await call_next(request)
