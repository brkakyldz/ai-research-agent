"""Typed settings, read once from the environment and cached.

Every knob the operator is expected to touch lives here and in the environment
template; nothing else in the codebase reads `os.environ`. One door, for one
reason: "where did this setting come from" should always be answered by a single
file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Language = Literal["tr", "en"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- Secrets --------------------------------------------------------------
    openai_api_key: str = ""
    tavily_api_key: str = ""

    # -- LLM ------------------------------------------------------------------
    # These three are defaults, not settings (ADR 0020). Which model runs is an
    # argument to the work - chosen in the confirmation on /runs, or with the
    # CLI's `--model` flags - and these say what a press or a command that names
    # nothing falls back to. `pipeline.pricing.PRICES` is the menu they are
    # chosen from and the table they are costed at.
    openai_model: str = "gpt-5.6-luna"
    # Its own knob: if Turkish quality disappoints, only this node moves up to
    # terra and ranking stays on luna (ADR 0001).
    openai_model_summarize: str = "gpt-5.6-luna"
    # The grounding judge (`ainews eval judge`), one tier up from the model it
    # checks: a judge no stronger than the summariser mostly agrees with it.
    # Never used by the pipeline; sampled, so ~$2/month at one judged run a day.
    openai_model_judge: str = "gpt-5.6-terra"
    openai_timeout_seconds: int = 60
    openai_max_retries: int = 5
    # Send fan-out batch size; keeps concurrent requests under the rate limit.
    summarize_batch_size: int = Field(default=10, ge=1, le=50)

    # -- Digest ---------------------------------------------------------------
    digest_language: Language = "tr"
    digest_top_n: int = Field(default=15, ge=1, le=100)
    dedupe_score_threshold: int = Field(default=85, ge=0, le=100)
    dedupe_lookback_hours: int = Field(default=72, ge=1)
    tavily_daily_cap: int = Field(default=30, ge=0)
    source_max_failures: int = Field(default=5, ge=1)
    # Some feeds serve their whole archive (OpenAI ships 1169 entries, Hugging
    # Face 859). Without a horizon the first collect would summarise years of
    # news; with one, the backlog is skipped and only current items land.
    collect_max_age_days: int = Field(default=7, ge=1, le=365)

    # -- Collection, and the advice to run --------------------------------------
    # Only the collect poll is on a clock. The digest is started by hand (ADR
    # 0015), so what used to be a cron expression is now an interval the /runs
    # page counts down from the last successful digest - advice, not a trigger.
    scheduler_enabled: bool = True
    collect_interval_hours: int = Field(default=3, ge=1, le=24)
    digest_suggest_after_hours: int = Field(default=24, ge=1, le=168)
    timezone: str = "Europe/Istanbul"

    # -- Storage --------------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./data/app.db"

    # -- Tracing ----------------------------------------------------------------
    # Off unless asked for. The endpoint's default is the host's view of the
    # Phoenix container; compose overrides it with the service name, the same
    # way it overrides DATABASE_URL.
    phoenix_enabled: bool = False
    phoenix_endpoint: str = "http://localhost:6006/v1/traces"

    # -- Web ------------------------------------------------------------------
    # Loopback, because there is no login on this dashboard. `ainews serve` on a
    # laptop used to bind every interface, which put the day's bulletin and the
    # button that spends money on whatever café network the laptop was joined to.
    # The container is the case that needs 0.0.0.0 - the process has to be
    # reachable from outside its own namespace - and the Dockerfile's CMD passes
    # `--host 0.0.0.0` explicitly, so it is a property of that entry point rather
    # than of everyone's default.
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    environment: Literal["development", "production"] = "development"

    @field_validator("openai_api_key", "tavily_api_key", mode="before")
    @classmethod
    def _blank_placeholders(cls, v: object) -> object:
        """A key still holding the template's `xxxx` is not a configured key."""
        if isinstance(v, str) and ("xxxx" in v or v.strip() == ""):
            return ""
        return v

    @property
    def llm_configured(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def tavily_configured(self) -> bool:
        return bool(self.tavily_api_key)

    @property
    def sqlite_path(self) -> Path:
        """Filesystem path behind `database_url`.

        Two callers: the WAL pragmas at connection setup, and `checkpoint_path`,
        which puts the graph's checkpoint file beside it. It said "and backups"
        until 2026-09-08 and there has never been a backup command; getting the
        database out of the container is `docker cp`, which `docker-compose.yml`
        documents where it explains the named volume.
        """
        _, _, tail = self.database_url.partition(":///")
        return (PROJECT_ROOT / tail).resolve() if tail.startswith("./") else Path(tail)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
