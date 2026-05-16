from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from tea_scrapers.config import VendorConfig, load_shopify_vendors


def _write_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "vendors.yaml"
    p.write_text(dedent(body), encoding="utf-8")
    return p


def test_loads_single_vendor(tmp_path: Path):
    path = _write_yaml(
        tmp_path,
        """
        shopify_vendors:
          yunnan_sourcing_us:
            display_name: "Yunnan Sourcing USA"
            base_url: "https://yunnansourcing.us"
            rate_limit_rps: 2
        """,
    )
    vendors = load_shopify_vendors(path)
    assert set(vendors) == {"yunnan_sourcing_us"}
    cfg = vendors["yunnan_sourcing_us"]
    assert isinstance(cfg, VendorConfig)
    assert cfg.source_key == "yunnan_sourcing_us"
    assert cfg.display_name == "Yunnan Sourcing USA"
    assert cfg.base_url == "https://yunnansourcing.us"
    assert cfg.rate_limit_rps == 2.0


def test_rate_limit_defaults_to_2_when_omitted(tmp_path: Path):
    path = _write_yaml(
        tmp_path,
        """
        shopify_vendors:
          some_vendor:
            display_name: "Some Vendor"
            base_url: "https://example.test"
        """,
    )
    vendors = load_shopify_vendors(path)
    assert vendors["some_vendor"].rate_limit_rps == 2.0


def test_empty_base_url_raises(tmp_path: Path):
    path = _write_yaml(
        tmp_path,
        """
        shopify_vendors:
          bad:
            display_name: "Bad"
            base_url: ""
            rate_limit_rps: 1
        """,
    )
    with pytest.raises(ValueError):
        load_shopify_vendors(path)


def test_negative_rate_limit_raises(tmp_path: Path):
    path = _write_yaml(
        tmp_path,
        """
        shopify_vendors:
          bad:
            display_name: "Bad"
            base_url: "https://example.test"
            rate_limit_rps: -1
        """,
    )
    with pytest.raises(ValueError):
        load_shopify_vendors(path)


def test_missing_shopify_vendors_key_raises(tmp_path: Path):
    path = _write_yaml(
        tmp_path,
        """
        not_shopify:
          irrelevant: true
        """,
    )
    with pytest.raises(ValueError) as exc_info:
        load_shopify_vendors(path)
    assert "shopify_vendors" in str(exc_info.value)


def test_missing_required_field_raises(tmp_path: Path):
    path = _write_yaml(
        tmp_path,
        """
        shopify_vendors:
          incomplete:
            base_url: "https://example.test"
        """,
    )
    with pytest.raises(ValueError):
        load_shopify_vendors(path)


def test_round_trip_against_real_vendors_yaml():
    # Resolve relative to this file so the test is CWD-independent.
    real = Path(__file__).resolve().parents[2] / "config" / "vendors.yaml"
    assert real.exists(), f"expected real vendor config at {real}"
    vendors = load_shopify_vendors(real)
    expected = {"yunnan_sourcing_us", "yunnan_sourcing_com", "white2tea", "crimson_lotus"}
    assert set(vendors) == expected
    assert "bitterleaf" not in vendors
    for cfg in vendors.values():
        assert cfg.base_url.startswith("https://")
        assert cfg.rate_limit_rps > 0
