"""Curated producer-alias loader (ROADMAP H.2b part 2). No DB."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from tea_scrapers.normalize.producer_aliases import (
    DEFAULT_ALIAS_PATH,
    alias_keys,
    load_producer_aliases,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "producer_aliases"
    / "sample_producer_aliases.yaml"
)
REAL = Path(__file__).resolve().parents[2] / DEFAULT_ALIAS_PATH


def test_alias_keys_add_pinyin_for_hanzi_only() -> None:
    assert alias_keys("大益") == ["大益", "da yi"]
    assert alias_keys("  Menghai   Tea Factory ") == ["menghai tea factory"]


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("Dayi", "Menghai Tea Factory"),
        ("DaYi", "Menghai Tea Factory"),
        ("Da Yi", "Menghai Tea Factory"),
        ("Da-Yi", "Menghai Tea Factory"),  # skeleton
        ("大益", "Menghai Tea Factory"),
        ("勐海茶厂", "Menghai Tea Factory"),
        ("Menghai Tea Factory (Da Yi Brand)", "Menghai Tea Factory"),
        ("下关茶厂", "Xiaguan Tea Factory"),
        ("W2T", "white2tea"),
        ("White 2 Tea", "white2tea"),
        ("White2Tea", "white2tea"),  # skeleton of "White 2 Tea"
        ("Crimson Lotus Tea", "Crimson Lotus Tea"),
    ],
)
def test_resolve(raw: str, canonical: str) -> None:
    assert load_producer_aliases(FIXTURE).resolve(raw) == canonical


def test_unlisted_names_do_not_resolve() -> None:
    tbl = load_producer_aliases(FIXTURE)
    assert tbl.resolve("Menghai Tian Di Ren Tea Factory") is None
    assert tbl.resolve("Xinghai Tea Factory") is None
    assert tbl.resolve("Da-Yi", use_skeleton=False) is None


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "aliases.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_alias_under_two_canonicals_is_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "version: 1\nproducers:\n"
        "  - canonical: Menghai Tea Factory\n    aliases: [Dayi]\n"
        "  - canonical: Menghai Tian Di Ren Tea Factory\n    aliases: [DAYI]\n",
    )
    with pytest.raises(ValueError, match="dayi"):
        load_producer_aliases(p)


def test_schema_is_strict(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        load_producer_aliases(_write(tmp_path, "version: 2\nproducers: []\n"))
    with pytest.raises(ValidationError):
        load_producer_aliases(
            _write(tmp_path, "version: 1\nproducers:\n  - canonical: X\n    alias: [Y]\n")
        )


def test_skeleton_collision_across_canonicals_is_dropped_not_guessed(tmp_path: Path) -> None:
    # Distinct normalized keys ("hai lang hao" / "hailanghao"), same skeleton.
    p = _write(
        tmp_path,
        "version: 1\nproducers:\n"
        "  - canonical: Hai Lang Hao\n"
        "  - canonical: Hailanghao\n",
    )
    tbl = load_producer_aliases(p)
    assert tbl.ambiguous_skeletons == {"hailanghao"}
    assert tbl.resolve("Hai Lang Hao") == "Hai Lang Hao"  # exact key still works
    assert tbl.resolve("Hai-Lang-Hao") is None  # skeleton-only → refuse to guess


@pytest.mark.skipif(not REAL.exists(), reason="curated config/producer_aliases.yaml absent")
def test_curated_file_loads_and_is_unambiguous() -> None:
    tbl = load_producer_aliases(REAL)
    assert tbl.entries
    assert tbl.ambiguous_skeletons == set()


def test_cli_refuses_malformed_alias_file(tmp_path: Path) -> None:
    from tea_scrapers.cli import cli

    bad = _write(
        tmp_path,
        "version: 1\nproducers:\n"
        "  - canonical: A Factory\n    aliases: [Shared]\n"
        "  - canonical: B Factory\n    aliases: [shared]\n",
    )
    res = CliRunner().invoke(
        cli, ["normalize", "--since", "2026-01-01", "--producer-aliases", str(bad)]
    )
    assert res.exit_code == 2
