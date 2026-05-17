"""Unit tests for the `steepster:` block in `config/vendors.yaml`.

Spec reference: specs/tea_scrapers_v1_spec.md §6.2 + §12 step-7 architectural
decision (Steepster config lives alongside Shopify vendors rather than in a
separate `config/sources.yaml`).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from tea_scrapers.config import SteepsterConfig, load_steepster_config


def _write_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "vendors.yaml"
    p.write_text(dedent(body), encoding="utf-8")
    return p


def test_loads_full_steepster_block(tmp_path: Path):
    path = _write_yaml(
        tmp_path,
        """
        shopify_vendors: {}
        steepster:
          base_url: "https://steepster.com"
          rate_limit_rps: 0.1
          timeout_seconds: 60
          vendor_slugs:
            - "yunnan-sourcing"
            - "white2tea"
        """,
    )
    cfg = load_steepster_config(path)
    assert isinstance(cfg, SteepsterConfig)
    assert cfg.base_url == "https://steepster.com"
    assert cfg.rate_limit_rps == 0.1
    assert cfg.timeout_seconds == 60.0
    assert cfg.vendor_slugs == ["yunnan-sourcing", "white2tea"]


def test_absent_block_returns_defaults(tmp_path: Path):
    """A YAML with no `steepster:` key must yield defaults, not error.

    Rationale: a fresh checkout that hasn't customized vendors.yaml should
    not break the `tea-scrape ingest steepster` CLI's load path; the CLI
    surfaces the empty-slugs case as a UsageError downstream.
    """
    path = _write_yaml(tmp_path, "shopify_vendors: {}\n")
    cfg = load_steepster_config(path)
    assert cfg.base_url == "https://steepster.com"  # class default
    assert cfg.rate_limit_rps == 0.1
    assert cfg.timeout_seconds == 60.0
    assert cfg.vendor_slugs == []


def test_zero_rate_limit_rejected(tmp_path: Path):
    """Zero or negative rps would deadlock the rate limiter — reject loudly."""
    path = _write_yaml(
        tmp_path,
        """
        steepster:
          rate_limit_rps: 0
          vendor_slugs: []
        """,
    )
    with pytest.raises(ValueError) as exc:
        load_steepster_config(path)
    assert "rate_limit_rps" in str(exc.value)


def test_negative_timeout_rejected(tmp_path: Path):
    path = _write_yaml(
        tmp_path,
        """
        steepster:
          rate_limit_rps: 0.1
          timeout_seconds: -1
          vendor_slugs: []
        """,
    )
    with pytest.raises(ValueError) as exc:
        load_steepster_config(path)
    assert "timeout_seconds" in str(exc.value)


def test_non_mapping_block_rejected(tmp_path: Path):
    path = _write_yaml(
        tmp_path,
        """
        steepster: "not-a-mapping"
        """,
    )
    with pytest.raises(ValueError):
        load_steepster_config(path)


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(ValueError) as exc:
        load_steepster_config(tmp_path / "does-not-exist.yaml")
    assert "not found" in str(exc.value)


def test_env_override_for_rate_limit(tmp_path: Path, monkeypatch):
    """`STEEPSTER_RATE_LIMIT_RPS` overrides the YAML default.

    Documented as a test-and-ops escape hatch (see config.py inline
    comment). The override path is the critical mechanism that lets
    VCR-replay integration tests finish in seconds rather than the
    crawl-delay-bounded wall clock walk.
    """
    path = _write_yaml(
        tmp_path,
        """
        steepster:
          rate_limit_rps: 0.1
          vendor_slugs: ["yunnan-sourcing"]
        """,
    )
    monkeypatch.setenv("STEEPSTER_RATE_LIMIT_RPS", "50")
    cfg = load_steepster_config(path)
    assert cfg.rate_limit_rps == 50.0
    # Slug list and other fields unchanged.
    assert cfg.vendor_slugs == ["yunnan-sourcing"]


def test_env_override_invalid_float_raises(tmp_path: Path, monkeypatch):
    path = _write_yaml(
        tmp_path,
        """
        steepster:
          rate_limit_rps: 0.1
          vendor_slugs: []
        """,
    )
    monkeypatch.setenv("STEEPSTER_RATE_LIMIT_RPS", "not-a-number")
    with pytest.raises(ValueError):
        load_steepster_config(path)


def test_round_trip_against_real_vendors_yaml():
    """Sanity-check that the *real* config/vendors.yaml loads.

    Catches accidental schema drift between the live config and the loader.
    """
    real = Path(__file__).resolve().parents[2] / "config" / "vendors.yaml"
    assert real.exists(), f"expected real vendor config at {real}"
    cfg = load_steepster_config(real)
    assert cfg.base_url == "https://steepster.com"
    # rate_limit_rps must honor crawl-delay: 10 (spec §6.2).
    assert cfg.rate_limit_rps == pytest.approx(0.1)
    # The V1 allowlist must include the Shopify-quartet overlaps.
    assert "yunnan-sourcing" in cfg.vendor_slugs
    assert "white2tea" in cfg.vendor_slugs
    assert "crimson-lotus-tea" in cfg.vendor_slugs
