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

    # HITL plan-approval (PRD §6.1, §13 P2; slice 12). On → the graph interrupts
    # after `plan` for human review/edit of the questions before research.
    hitl_plan_approval: bool = True

    # --- Cross-session memory (PRD §7.1, §16; slice 11) -----------------
    memory_ttl_durable_seconds: float = 14 * 86400  # 14d — Overview/Products warm-start
    memory_ttl_volatile_seconds: float = 86400  # 1d — Signals/Risks re-fetched
    memory_match_max_distance: float = 0.85  # cosine distance to bind memory→question

    # --- Durable call ledger (PRD §6.5; slice 6) -------------------------
    ledger_lease_seconds: float = 30.0  # reservation ownership before takeover
    ledger_heartbeat_seconds: float = 10.0  # owner lease-extension cadence
    ledger_poll_seconds: float = 0.25  # waiter re-check cadence (single-flight)
    ledger_wait_timeout_seconds: float = 120.0  # max single-flight wait

    # --- Providers (real backends wired in slice 7) ----------------------
    openai_api_key: str | None = None
    tavily_api_key: str | None = None
    openai_base_url: str | None = None  # optional gateway/proxy override
    verify_models_on_boot: bool = True  # fail-fast on an unknown model id (§16)

    # Per-node model ids (config placeholders verified at boot; §0/§16). The
    # analyze synthesis step can step up to a stronger model via config alone (§15).
    model_plan: str = "gpt-5-nano"
    model_extract: str = "gpt-5-nano"
    model_analyze: str = "gpt-5"
    model_quality: str = "gpt-5-nano"
    model_chat: str = "gpt-5-nano"
    model_embedding: str = "text-embedding-3-small"

    # Per-node output token caps (cost control, §6.4).
    max_output_tokens_plan: int = 1024
    max_output_tokens_extract: int = 512
    max_output_tokens_analyze: int = 4096
    max_output_tokens_quality: int = 512
    max_output_tokens_chat: int = 1024

    # Network-bound node retries + SSRF-guarded fetch (§7, §11).
    retry_max_attempts: int = 3
    search_depth: Literal["basic", "advanced"] = "basic"
    fetch_enrich: bool = True
    fetch_timeout_seconds: float = 10.0
    fetch_max_bytes: int = 2_000_000
    allow_private_fetch: bool = False  # SSRF escape hatch (tests/dev only)

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
