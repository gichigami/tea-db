"""Application settings.

Loaded from environment variables (and optionally a local `.env` file).
Spec reference: specs/tea_scrapers_v1_spec.md §4 (Configuration).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER_UA_MARKERS: tuple[str, ...] = (
    "github.com/gary/tea",
    "contact: gary@",
)


def _reject_placeholder_ua(value: str, *, field: str) -> str:
    """Reject the well-known placeholder ``User-Agent`` substrings.

    The class defaults for ``user_agent`` and ``reddit_user_agent`` are
    intentionally invalid (they contain ``github.com/gary/tea`` and
    ``contact: gary@``). An operator who forgets to override them in
    ``.env`` fails loudly here at ``Settings()`` instantiation rather than
    silently tripping Shopify edge mitigation on the first real scrape —
    see specs/tea_scrapers_v1_spec.md §12 (Shopify storefront bot mitigation).
    """
    lowered = value.lower()
    for marker in _PLACEHOLDER_UA_MARKERS:
        if marker in lowered:
            raise ValueError(
                f"{field} contains the placeholder substring {marker!r}. "
                f"Set a real {field.upper()} in .env per .env.example "
                f"(spec §12)."
            )
    return value


class Settings(BaseSettings):
    """Process-wide settings.

    Secrets and connection strings come from env vars; per-vendor parameters
    (URLs, rate limits) live in `config/vendors.yaml` — distinct sources of
    config that should not be mixed (spec §4).
    """

    database_url: str = "postgresql+psycopg://localhost/tea"
    raw_data_dir: Path = Path("data/raw")
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "tea-rec-engine/0.1 (contact: gary@...)"
    user_agent: str = (
        "tea-rec-engine/0.1 (https://github.com/gary/tea; contact: gary@...)"
    )
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("user_agent")
    @classmethod
    def _validate_user_agent(cls, v: str) -> str:
        return _reject_placeholder_ua(v, field="user_agent")

    @field_validator("reddit_user_agent")
    @classmethod
    def _validate_reddit_user_agent(cls, v: str) -> str:
        return _reject_placeholder_ua(v, field="reddit_user_agent")


def get_settings() -> Settings:
    """Return a fresh `Settings` instance.

    Callers that need the same instance throughout a process can cache it
    themselves; we keep the getter side-effect-free so tests can override
    environment cleanly.
    """
    return Settings()


# ---------------------------------------------------------------------------
# Vendor config (config/vendors.yaml) — distinct from env-driven Settings
# ---------------------------------------------------------------------------


class VendorConfig(BaseModel):
    """A single Shopify vendor's static configuration.

    Spec reference: specs/tea_scrapers_v1_spec.md §6.1.
    """

    source_key: str
    display_name: str
    base_url: str = Field(min_length=1)
    rate_limit_rps: float = 2.0


def load_shopify_vendors(path: Path | None = None) -> dict[str, VendorConfig]:
    """Load `shopify_vendors` from a YAML file into a dict keyed by source_key.

    `path` defaults to ``Path("config/vendors.yaml")`` resolved relative to the
    current working directory — same convention as ``Settings.raw_data_dir``.
    Malformed entries (missing keys, empty `base_url`, negative
    `rate_limit_rps`) raise ``ValueError`` — never swallowed (spec §4, §11).
    """
    resolved_path = path or Path("config/vendors.yaml")
    try:
        raw_text = resolved_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"vendor config not found at {resolved_path}") from exc

    parsed: Any = yaml.safe_load(raw_text)
    if not isinstance(parsed, dict):
        raise ValueError(
            f"{resolved_path}: expected top-level mapping, got {type(parsed).__name__}"
        )

    if "shopify_vendors" not in parsed:
        raise ValueError(f"{resolved_path}: missing required key 'shopify_vendors'")

    vendors_section = parsed["shopify_vendors"]
    if not isinstance(vendors_section, dict):
        raise ValueError(
            f"{resolved_path}: 'shopify_vendors' must be a mapping, "
            f"got {type(vendors_section).__name__}"
        )

    out: dict[str, VendorConfig] = {}
    for source_key, entry in vendors_section.items():
        if not isinstance(entry, dict):
            raise ValueError(
                f"{resolved_path}: vendor '{source_key}' must be a mapping, "
                f"got {type(entry).__name__}"
            )
        merged = {"source_key": source_key, **entry}
        try:
            vendor = VendorConfig(**merged)
        except ValidationError as exc:
            raise ValueError(
                f"{resolved_path}: vendor '{source_key}' is malformed: {exc}"
            ) from exc
        if vendor.rate_limit_rps < 0:
            raise ValueError(
                f"{resolved_path}: vendor '{source_key}' has negative rate_limit_rps "
                f"({vendor.rate_limit_rps})"
            )
        out[source_key] = vendor
    return out


__all__ = ["Settings", "get_settings", "VendorConfig", "load_shopify_vendors"]
