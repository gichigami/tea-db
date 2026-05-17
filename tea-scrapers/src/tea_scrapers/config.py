"""Application settings.

Loaded from environment variables (and optionally a local `.env` file).
Spec reference: specs/tea_scrapers_v1_spec.md §4 (Configuration).
"""

from __future__ import annotations

import os
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


class SteepsterConfig(BaseModel):
    """Configuration for the (single) Steepster source.

    Spec reference: specs/tea_scrapers_v1_spec.md §6.2.

    Unlike Shopify (one vendor per `source_key`), Steepster is one *source*
    that crawls many vendor slugs. `vendor_slugs` is the V1 allowlist
    (§6.2 "Vendor slugs to crawl") — scope-expansion past the allowlist is
    a tech-lead V1.1 decision (§12 "Steepster corpus scope expansion").

    `rate_limit_rps` defaults to 0.1 to honor robots.txt `Crawl-Delay: 10`
    (one request per 10 seconds; §6.2). `timeout_seconds` defaults to 60 to
    cover the slow first-hit render times observed on tea-detail pages
    (§6.2 "HTTP timeout ≥ 60 seconds").
    """

    base_url: str = Field(default="https://steepster.com", min_length=1)
    rate_limit_rps: float = 0.1
    timeout_seconds: float = 60.0
    vendor_slugs: list[str] = Field(default_factory=list)


def load_steepster_config(path: Path | None = None) -> SteepsterConfig:
    """Load the `steepster:` block from a YAML file.

    Same on-disk file as `load_shopify_vendors` (`config/vendors.yaml` by
    default); just a different top-level key. Absent block → return defaults
    so a fresh checkout doesn't error before the operator wires `.env`.
    Malformed values (negative rate, non-list slugs) raise ``ValueError``.
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

    section = parsed.get("steepster")
    if section is None:
        return SteepsterConfig()
    if not isinstance(section, dict):
        raise ValueError(
            f"{resolved_path}: 'steepster' must be a mapping, "
            f"got {type(section).__name__}"
        )

    try:
        cfg = SteepsterConfig(**section)
    except ValidationError as exc:
        raise ValueError(
            f"{resolved_path}: 'steepster' block is malformed: {exc}"
        ) from exc

    # Env override for the rate limit (and only the rate limit). Two
    # legitimate consumers:
    # 1. Integration tests replaying VCR cassettes — the rate limiter
    #    has no idea VCR is intercepting and would still sleep
    #    `1 / rate_limit_rps` between requests, making a 100-page cassette
    #    replay take 1000s.
    # 2. Operators who, with evidence and after V1.1, want to bump or
    #    drop the rate without editing the committed yaml.
    # YAML stays the polite-citizen default (0.1 rps = crawl-delay 10s).
    env_override = os.environ.get("STEEPSTER_RATE_LIMIT_RPS")
    if env_override is not None:
        try:
            cfg = cfg.model_copy(update={"rate_limit_rps": float(env_override)})
        except ValueError as exc:
            raise ValueError(
                f"STEEPSTER_RATE_LIMIT_RPS={env_override!r} is not a float"
            ) from exc

    if cfg.rate_limit_rps <= 0:
        raise ValueError(
            f"{resolved_path}: 'steepster' rate_limit_rps must be > 0 "
            f"(got {cfg.rate_limit_rps})"
        )
    if cfg.timeout_seconds <= 0:
        raise ValueError(
            f"{resolved_path}: 'steepster' timeout_seconds must be > 0 "
            f"(got {cfg.timeout_seconds})"
        )
    return cfg


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


__all__ = [
    "Settings",
    "get_settings",
    "VendorConfig",
    "load_shopify_vendors",
    "SteepsterConfig",
    "load_steepster_config",
]
