"""One place that decides what logs look like.

Plain text, not JSON: this runs on one machine and is read by one person in a
terminal, so a line a human can scan beats a line a log shipper can parse. There
is no log shipper here.
"""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)-28s %(message)s", "%H:%M:%S")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # These are chatty at INFO and say nothing we act on.
    for noisy in ("httpx", "httpcore", "openai", "apscheduler.executors.default", "trafilatura"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True
