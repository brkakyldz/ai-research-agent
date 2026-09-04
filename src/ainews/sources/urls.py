"""URL canonicalisation - the first and cheapest layer of deduplication.

The same article reaches us from several feeds with different decoration:
`?utm_source=`, a trailing slash, `http` vs `https`, `m.` or `amp.` hostnames,
a `#section` fragment. Reducing all of those to one string lets the database's
unique index do the work, before any fuzzy matching or any LLM call.

Only *tracking* parameters are stripped. A query string can be load-bearing
(`?id=44001` on Hacker News, `?v=` on YouTube), so the rule is a denylist of
known-decorative keys, never an allowlist of keys we happen to recognise.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from url_normalize import url_normalize

# Prefixes rather than exact names: `utm_*` alone has a dozen members, and
# vendors keep adding them.
TRACKING_PREFIXES = (
    "utm_",
    "ga_",
    "hsa_",
    "mc_",
    "pk_",
    "vero_",
    "_hs",
)

TRACKING_KEYS = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "twclid",
        "igshid",
        "mkt_tok",
        "ref",
        "referrer",
        "source",
        "cmpid",
        "ncid",
        "at_medium",
        "at_campaign",
        "spm",
        "share_id",
        "guccounter",
        "guce_referrer",
        "guce_referrer_sig",
    }
)

# Hostname prefixes that serve the same document as the bare host.
MIRROR_PREFIXES = ("www.", "m.", "amp.", "mobile.")


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered in TRACKING_KEYS or lowered.startswith(TRACKING_PREFIXES)


def _canonicalise(raw: str) -> str:
    candidate = raw.strip()
    try:
        normalized = url_normalize(candidate)
    except Exception:
        normalized = candidate

    parts = urlsplit(normalized)
    scheme = "https" if parts.scheme in ("", "http", "https") else parts.scheme

    host = parts.hostname or ""
    for prefix in MIRROR_PREFIXES:
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"

    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)]
    )

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    # The fragment is always presentational for our purposes.
    return urlunsplit((scheme, host, path, query, ""))


def canonical_url(raw: str) -> str:
    """Return a stable key for `raw`. Never raises; falls back to the input.

    The guard is not defensive habit. `urlsplit` parses lazily, so a link like
    `javascript:void(0)` - which real feeds do carry - only explodes when
    `.port` tries to cast `void(0)` to an integer, several lines after the parse
    looked fine. One bad link in one feed must not take down a collection run
    across eighteen of them.
    """
    if not raw:
        return ""
    try:
        return _canonicalise(raw)
    except Exception:
        return raw.strip()
