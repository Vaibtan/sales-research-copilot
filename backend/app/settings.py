"""Application configuration (12-factor via ``pydantic-settings``).

All runtime configuration is read from the environment (and a local ``.env`` in
development). No secrets are committed — ``.env.example`` documents every key.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Typed, validated application settings.

    Field names map to upper-cased environment variables (``app_name`` →
    ``APP_NAME``). Connection strings are kept as plain strings so they pass
    through to the underlying drivers (psycopg / redis) untouched — validity is
    proven by the live ``/healthz`` checks rather than by URL re-rendering.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App -------------------------------------------------------------
    app_name: str = "sales-research-copilot"
    environment: Environment = "development"
    log_level: LogLevel = "INFO"
    log_json: bool = True

    # --- Datastores ------------------------------------------------------
    # psycopg-compatible DSN, e.g. postgresql://user:pass@host:5432/db
    database_url: str = "postgresql://copilot:copilot@localhost:5432/copilot"
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10

    # redis URL, e.g. redis://host:6379/0
    redis_url: str = "redis://localhost:6379/0"

    # --- HTTP ------------------------------------------------------------
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # --- Workflow (PRD §16; quality loop wired in slice 4) ---------------
    quality_threshold: float = 0.7  # τ — re-research below this score
    max_extra_passes: int = 1  # bounded loop-back budget

    # --- Providers (unused in slice 1; wired in slice 7) -----------------
    openai_api_key: str | None = None
    tavily_api_key: str | None = None

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv_origins(cls, value: object) -> object:
        """Accept either a JSON array or a comma-separated string from the env."""
        if isinstance(value, str) and not value.strip().startswith("["):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
