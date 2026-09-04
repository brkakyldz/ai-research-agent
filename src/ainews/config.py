"""Typed settings, loaded once from the environment (and a dotenv file in development).

Every knob the operator is expected to touch lives here and in the environment
template; nothing else in the codebase reads `os.environ` directly.
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
    openai_model: str = "gpt-5.6-luna"
    openai_model_summarize: str = "gpt-5.6-luna"
    openai_timeout_seconds: int = 60
    openai_max_retries: int = 5
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

    # -- Scheduling -----------------------------------------------------------
    scheduler_enabled: bool = True
    collect_interval_hours: int = Field(default=3, ge=1, le=24)
    digest_cron_hour: int = Field(default=7, ge=0, le=23)
    digest_cron_minute: int = Field(default=0, ge=0, le=59)
    timezone: str = "Europe/Istanbul"

    # -- Storage --------------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./data/app.db"

    # -- Web ------------------------------------------------------------------
    host: str = "0.0.0.0"  # Yerel arac: baglanti konteyner icinde, disari acilan port compose'da.
    port: int = 8000
    log_level: str = "INFO"
    environment: Literal["development", "production"] = "development"

    @field_validator("openai_api_key", "tavily_api_key", mode="before")
    @classmethod
    def _blank_placeholders(cls, v: object) -> object:
        """Treat the template's `xxxx` placeholders as "not configured"."""
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
        """Filesystem path behind `database_url`, for WAL setup and backups."""
        _, _, tail = self.database_url.partition(":///")
        return (PROJECT_ROOT / tail).resolve() if tail.startswith("./") else Path(tail)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
