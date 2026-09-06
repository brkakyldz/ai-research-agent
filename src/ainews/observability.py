"""Optional OpenTelemetry tracing into a local Phoenix.

Off by default and off in every test. When `PHOENIX_ENABLED` is set, one call
to `LangChainInstrumentor` patches LangChain's callback layer for the whole
process, and every runnable it drives - the graph's nodes, the structured-output
chain, the `ChatOpenAI` call underneath - emits a span carrying its input,
output, token counts and duration. Nothing in the pipeline is touched: the
instrumentation is global, so there is no seam to thread through `make_llm`.

Two properties are deliberate. The imports are inside `_load`, so a run with
tracing off never loads the Phoenix client or the OTLP exporter, and no
instrumentation is applied. (The OpenTelemetry SDK itself does get imported
once the `obs` group is installed - LangGraph reaches for it on its own when it
is present - but with no exporter configured that costs an import and nothing
else.) And every failure here is swallowed with a log line: a development
instrument that can stop a digest is worse than no instrument (ADR 0018).
"""

from __future__ import annotations

import logging
from typing import Any

from ainews.config import Settings, get_settings

log = logging.getLogger(__name__)

_ENABLED = False


def _load() -> tuple[Any, Any] | None:
    """The optional imports, behind one seam.

    A function rather than a module-level try/except for two reasons: tracing
    off means OpenTelemetry is never imported at all, and a test can replace
    this without the packages being installed.
    """
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor
        from phoenix.otel import register
    except ImportError:
        return None
    return register, LangChainInstrumentor


def enable_tracing(settings: Settings | None = None) -> bool:
    """Point LangChain's spans at Phoenix. Returns whether tracing is now on.

    Idempotent, like `configure_logging`: the CLI and the web app both call it
    and only the first call instruments.
    """
    global _ENABLED
    settings = settings or get_settings()

    if not settings.phoenix_enabled:
        return False
    if _ENABLED:
        return True

    loaded = _load()
    if loaded is None:
        log.warning(
            "PHOENIX_ENABLED is set but the tracing packages are missing. "
            "Install them with: uv sync --group obs"
        )
        return False
    register, LangChainInstrumentor = loaded

    try:
        provider = register(
            project_name="ainews",
            endpoint=settings.phoenix_endpoint,
            # The digest opens one branch per candidate in a single superstep -
            # 136 on the run this was sized against. `register` defaults to a
            # simple processor that exports each span as it ends, which would
            # put an HTTP round trip inside every summarize branch. Batching
            # moves the export to a background thread, which is the only reason
            # a hundred-wide fan-out can be traced without paying for it.
            batch=True,
            verbose=False,
        )
        LangChainInstrumentor().instrument(tracer_provider=provider)
    except Exception:  # a dev instrument never breaks a run
        log.warning("tracing could not be enabled; continuing without it", exc_info=True)
        return False

    _ENABLED = True
    log.info("tracing enabled, sending spans to %s", settings.phoenix_endpoint)
    return True
