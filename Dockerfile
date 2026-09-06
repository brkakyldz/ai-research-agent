# syntax=docker/dockerfile:1

# Build and runtime in one stage: uv installs from the lockfile into a venv that
# the final image runs from, and there is nothing to compile that would justify
# carrying a second stage.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first: this layer is cached until the lockfile changes, so an
# edit to src/ rebuilds in seconds.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --group obs

COPY src/ ./src/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group obs

# The database lives on a mounted volume; the image ships none.
RUN mkdir -p /app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# One worker, deliberately: ADR 0004 (a second worker means a second scheduler
# and a duplicated 07:00 digest) and ADR 0003 (SQLite has one writer).
CMD ["uvicorn", "ainews.web.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
