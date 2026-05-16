"""Application settings.

Loaded from environment variables (and optionally a local `.env` file).
Spec reference: specs/tea_scrapers_v1_spec.md §4 (Configuration).
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide settings.

    Secrets and connection strings come from env vars; per-vendor parameters
    (URLs, rate limits) live in `config/vendors.yaml` — distinct sources of
    config that should not be mixed (spec §4).
    """

    database_url: str = "postgresql://localhost/tea"
    raw_data_dir: Path = Path("data/raw")
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "tea-rec-engine/0.1 (contact: gary@...)"
    user_agent: str = (
        "tea-rec-engine/0.1 (https://github.com/gary/tea; contact: gary@...)"
    )
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


def get_settings() -> Settings:
    """Return a fresh `Settings` instance.

    Callers that need the same instance throughout a process can cache it
    themselves; we keep the getter side-effect-free so tests can override
    environment cleanly.
    """
    return Settings()
