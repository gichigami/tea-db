#!/usr/bin/env python
"""Build the H.1 hand-labeled canonical-matcher pair set (deterministic, offline).

Reads ONLY committed repo data — the four Shopify ``/products.json`` VCR
cassettes, the gzipped Steepster cassette, the golden JSONL fixtures and
``config/vendors.yaml`` — and writes::

    tests/fixtures/matching/product_pairs.jsonl
    tests/fixtures/matching/producer_pairs.jsonl
    tests/fixtures/matching/low_confidence_review.csv

No network. Same inputs → byte-identical outputs (sampling uses sha256 order,
never ``random``). Labels are AGENT-PROPOSED (``review_status=agent_proposed``)
pending owner review; see ``tests/fixtures/matching/README.md``.

Blocking attributes come from the existing mapper
(:func:`tea_scrapers.normalize.shopify_mapper.map_payload_to_products`). A
Shopify side picks one variant weight (default: the largest parsed weight; an
explicit weight must be one the mapper actually parsed). Steepster sides run
the same mapper over a ``{"title": name}`` stub, so year/weight come from the
title regexes exactly as they would for a Shopify title.

Provenance rule (enforced here, validated by the pydantic schema):
``cassette`` = both names verbatim; ``cassette_derived`` = one name rewritten
with a token that is *attested* somewhere in the repo corpus (checked); a
rewrite whose token is not attested must be declared ``synthetic``.

Usage::

    python scripts/build_matcher_pairs.py            # write fixtures
    python scripts/build_matcher_pairs.py --check    # verify fixtures are current
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import itertools
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from selectolax.parser import HTMLParser

from tea_scrapers.eval.pairs import LabeledPair, PairSide
from tea_scrapers.normalize.shopify_mapper import map_payload_to_products, normalize_name

ROOT = Path(__file__).resolve().parents[1]
CASSETTES = ROOT / "tests" / "fixtures" / "cassettes"
GOLDEN = ROOT / "tests" / "fixtures" / "golden"
OUT_DIR = ROOT / "tests" / "fixtures" / "matching"
VENDORS_YAML = ROOT / "config" / "vendors.yaml"

SHOPIFY = {
    "us": "yunnan_sourcing_us",
    "com": "yunnan_sourcing_com",
    "w2t": "white2tea",
    "cl": "crimson_lotus",
}
STEEPSTER_CASSETTE = "steepster_crimson_lotus_tea.yaml.gz"
STEEPSTER_COMPANY = "Crimson Lotus Tea"
SEATTLE = " :: Seattle Inventory"
AUTO: Any = object()
NONE: Any = "none"  # explicit "weight is None" request for shop()

try:  # libyaml is ~10x faster on the 5 MB cassettes; fall back if absent.
    _Loader: Any = yaml.CSafeLoader
except AttributeError:  # pragma: no cover
    _Loader = yaml.SafeLoader


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# Catalog
# ----------------------------------------------------------------------


@dataclass
class ShopItem:
    vendor: str
    title: str
    pid: str
    producer: str | None
    year: int | None
    weights: list[int | None]
    product_type: str | None
    is_non_tea: bool
    body: str


@dataclass
class Catalog:
    shop: dict[str, dict[str, ShopItem]] = field(default_factory=dict)
    dups: dict[str, dict[str, list[ShopItem]]] = field(default_factory=dict)  # same-title extras
    steep: dict[str, str] = field(default_factory=dict)  # name -> tea id
    corpus: str = ""

    def item(self, v: str, title: str) -> ShopItem:
        try:
            return self.shop[v][title]
        except KeyError:
            raise SystemExit(f"title not in {SHOPIFY[v]} cassette: {title!r}") from None


def _load_yaml(path: Path) -> dict[str, Any]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return yaml.load(fh.read(), Loader=_Loader)
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)


def load_catalog() -> Catalog:
    cat = Catalog()
    corpus: list[str] = []
    for short, vendor in SHOPIFY.items():
        data = _load_yaml(CASSETTES / f"{vendor}_products.yaml")
        items: dict[str, ShopItem] = {}
        dups: dict[str, list[ShopItem]] = defaultdict(list)
        for inter in data["interactions"]:
            try:
                products = json.loads(inter["response"]["body"]["string"])["products"]
            except (KeyError, ValueError, TypeError):
                continue
            for p in products:
                mapped = map_payload_to_products(p)
                first = mapped[0]
                title = first.canonical_name
                item = ShopItem(
                    vendor=vendor,
                    title=title,
                    pid=str(p.get("id")),
                    producer=first.producer_hint.name if first.producer_hint else None,
                    year=first.harvest_year,
                    weights=[m.weight_grams for m in mapped],
                    product_type=first.tea_type,
                    is_non_tea=first.is_non_tea,
                    body=p.get("body_html") or "",
                )
                if title in items:  # same title listed twice: keep first, remember extras
                    dups[title].append(item)
                else:
                    items[title] = item
                corpus.extend([title, p.get("body_html") or "", " ".join(p.get("tags") or [])])
        cat.shop[short] = items
        cat.dups[short] = dict(dups)

    data = _load_yaml(CASSETTES / STEEPSTER_CASSETTE)
    for inter in data["interactions"]:
        if "/teas/" in inter["request"]["uri"]:
            tree = HTMLParser(inter["response"]["body"]["string"])
            corpus.extend(n.text(separator=" ") for n in tree.css("div.note"))
    tea_re = re.compile(r"^(?:https://steepster\.com)?/teas/crimson-lotus-tea/(\d+)-")
    suffix = "by " + STEEPSTER_COMPANY
    for inter in data["interactions"]:
        if "/companies/" not in inter["request"]["uri"]:
            continue
        tree = HTMLParser(inter["response"]["body"]["string"])
        for a in tree.css("a[href]"):
            m = tea_re.match(a.attributes.get("href") or "")
            txt = a.text(strip=True)
            if m and txt.endswith(suffix):
                name = txt[: -len(suffix)].strip()
                cat.steep.setdefault(name, m.group(1))
    corpus.extend(cat.steep)

    for g in sorted(GOLDEN.glob("*.jsonl")):
        corpus.append(g.read_text(encoding="utf-8"))
    corpus.append(VENDORS_YAML.read_text(encoding="utf-8"))
    cat.corpus = "\n".join(corpus)
    return cat


CAT: Catalog  # set in main()


# ----------------------------------------------------------------------
# Side constructors
# ----------------------------------------------------------------------


def shop(v: str, title: str, w: Any = AUTO, dup: int = 0) -> PairSide:
    it = CAT.item(v, title) if dup == 0 else CAT.dups[v][title][dup - 1]
    parsed = [x for x in it.weights if x is not None]
    if w is AUTO:
        weight = max(parsed) if parsed else None
    elif w == NONE:
        if None not in it.weights:
            raise SystemExit(f"{title!r}: no variant with unparsed weight")
        weight = None
    else:
        if w not in it.weights:
            raise SystemExit(f"{title!r}: weight {w} not among parsed {it.weights}")
        weight = w
    return PairSide(
        name=it.title,
        producer=it.producer,
        harvest_year=it.year,
        weight_grams=weight,
        source_ref=f"cassette:{it.vendor}_products.yaml#product_id={it.pid}",
        vendor=it.vendor,
    )


def steep(name: str) -> PairSide:
    if name not in CAT.steep:
        raise SystemExit(f"Steepster name not in cassette: {name!r}")
    stub = {"title": name, "vendor": STEEPSTER_COMPANY, "variants": [{"id": 0}]}
    m = map_payload_to_products(stub)[0]
    return PairSide(
        name=m.canonical_name,
        producer=STEEPSTER_COMPANY,
        harvest_year=m.harvest_year,
        weight_grams=m.weight_grams,
        source_ref=f"cassette:{STEEPSTER_CASSETTE}#tea_id={CAT.steep[name]}",
        vendor="steepster",
    )


def derive(base: PairSide, name: str, how: str, token: str, **attrs: Any) -> PairSide:
    """Rewrite ``base``'s name with an attested ``token`` (checked)."""
    if token not in CAT.corpus:
        raise SystemExit(f"derived token not attested in repo corpus: {token!r} ({name!r})")
    data = base.model_dump()
    data.update(attrs)
    data.update(name=name, source_ref=f"derived:{how}:{base.source_ref}")
    return PairSide(**data)


def synth(name: str, base: PairSide | None = None, **attrs: Any) -> PairSide:
    data = base.model_dump() if base is not None else {}
    data.update(attrs)
    data.update(name=name, source_ref="synthetic")
    data.setdefault("vendor", None)
    return PairSide(**data)


def prod_name(name: str, ref: str, vendor: str | None = None) -> PairSide:
    return PairSide(name=name, source_ref=ref, vendor=vendor)


def producer_of(v: str, title: str) -> PairSide:
    it = CAT.item(v, title)
    assert it.producer, title
    return prod_name(
        it.producer, f"cassette:{it.vendor}_products.yaml#product_id={it.pid}:producer", it.vendor
    )


def producer_token(v: str, title: str, token: str, how: str = "title_token") -> PairSide:
    """A producer string attested *inside a title* (or body) of a real product."""
    it = CAT.item(v, title)
    if token not in it.title and token not in it.body:
        raise SystemExit(f"{token!r} not in title/body of {title!r}")
    return prod_name(
        token, f"derived:{how}:cassette:{it.vendor}_products.yaml#product_id={it.pid}", it.vendor
    )


def producer_named(name: str) -> PairSide:
    """A producer string exactly as some cassette product carries it (first by pid)."""
    for short in ("us", "com", "w2t", "cl"):
        hits = sorted((it for it in CAT.shop[short].values() if it.producer == name),
                      key=lambda i: i.pid)
        if hits:
            it = hits[0]
            return prod_name(
                name, f"cassette:{it.vendor}_products.yaml#product_id={it.pid}:producer", it.vendor
            )
    raise SystemExit(f"producer not found in any cassette: {name!r}")


def producer_attested(token: str, ref: str) -> PairSide:
    if token not in CAT.corpus:
        raise SystemExit(f"producer token not attested: {token!r}")
    return prod_name(token, f"derived:attested:{ref}")


# ----------------------------------------------------------------------
# Pair accumulation
# ----------------------------------------------------------------------


@dataclass
class Spec:
    entity: str
    left: PairSide
    right: PairSide
    label: str
    conf: str
    category: str
    rationale: str
    tags: tuple[str, ...] = ()


SPECS: list[Spec] = []
_SEEN: set[tuple[str, frozenset[tuple[Any, ...]]]] = set()


def _side_key(s: PairSide) -> tuple[Any, ...]:
    # source_ref included: identical attribute tuples from two storefronts are a valid pair.
    return (s.name, s.producer, s.harvest_year, s.weight_grams, s.source_ref)


def add(
    left: PairSide,
    right: PairSide,
    label: str,
    conf: str,
    category: str,
    rationale: str,
    tags: tuple[str, ...] | list[str] = (),
    entity: str = "product",
) -> bool:
    key = (entity, frozenset({_side_key(left), _side_key(right)}))
    if key in _SEEN or _side_key(left) == _side_key(right):
        return False
    _SEEN.add(key)
    SPECS.append(Spec(entity, left, right, label, conf, category, rationale, tuple(tags)))
    return True


def addp(left: PairSide, right: PairSide, label: str, conf: str, category: str,
         rationale: str, tags: tuple[str, ...] | list[str] = ()) -> bool:
    return add(left, right, label, conf, category, rationale, tags, entity="producer")


def _source(spec: Spec) -> str:
    refs = (spec.left.source_ref, spec.right.source_ref)
    if any(r == "synthetic" for r in refs):
        return "synthetic"
    if any(r.startswith(("derived:", "config:")) for r in refs):
        return "cassette_derived"
    return "cassette"


def _auto_tags(spec: Spec) -> list[str]:
    tags = set(spec.tags)
    if spec.entity == "product":
        l, r = spec.left, spec.right
        if (l.weight_grams is None) != (r.weight_grams is None):
            tags.add("one_sided_weight")
        if (l.harvest_year is None) != (r.harvest_year is None):
            tags.add("one_sided_year")
        if l.vendor and r.vendor and l.vendor != r.vendor:
            tags.add("cross_source")
    return sorted(tags)


# ----------------------------------------------------------------------
# Helpers for the auto-mined families
# ----------------------------------------------------------------------


def common_weight(v1: str, t1: str, v2: str, t2: str) -> tuple[Any, Any]:
    a = {w for w in CAT.item(v1, t1).weights if w is not None}
    b = {w for w in CAT.item(v2, t2).weights if w is not None}
    c = a & b
    if c:
        return max(c), max(c)
    return AUTO, AUTO


def is_tea(it: ShopItem) -> bool:
    if it.is_non_tea:
        return False
    pt = (it.product_type or "").lower()
    return not any(
        k in pt
        for k in ("teaware", "pottery", "packaging", "accessor", "teapot", "tea pets",
                  "gift card", "options_hidden", "silver", "jianshui")
    )


YEAR_PREFIX = re.compile(r"^(?:19|20)\d\d\s+")


def strip_year(title: str) -> str:
    return YEAR_PREFIX.sub("", title)


# ======================================================================
# PRODUCT PAIRS
# ======================================================================


def build_products() -> None:  # a long, flat list of curated specs
    cl, w2t, us, com = "cl", "w2t", "us", "com"

    # ------------------------------------------------------------------
    # suffix_variant — Crimson Lotus ":: Seattle Inventory" twins (auto-mined)
    # ------------------------------------------------------------------
    seattle = sorted(t for t in CAT.shop[cl] if t.endswith(SEATTLE))
    exact_base, reordered = [], []

    def tok(s: str) -> tuple[str, ...]:
        return tuple(sorted(re.findall(r"[a-z0-9']+", s.lower())))

    by_tok: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for t in CAT.shop[cl]:
        if not t.endswith(SEATTLE):
            by_tok[tok(t)].append(t)
    for t in seattle:
        base = t[: -len(SEATTLE)].rstrip()
        if base in CAT.shop[cl]:
            exact_base.append((t, base))
        elif len(by_tok.get(tok(base), [])) == 1:
            reordered.append((t, by_tok[tok(base)][0]))
    # Cap the family so it doesn't swamp the positives: keep every word-order twin,
    # then a sha256-ordered sample of 18 same-weight + 12 weight-unparsed exact twins.
    chosen = list(reordered)
    budget = {True: 18, False: 12}
    for t, base in sorted(exact_base, key=lambda tb: _h("seattle|" + tb[0])):
        wa, wb = common_weight(cl, t, cl, base)
        key = shop(cl, t, wa).weight_grams == shop(cl, base, wb).weight_grams
        if budget[key] > 0:
            budget[key] -= 1
            chosen.append((t, base))
    for t, base in chosen:
        wa, wb = common_weight(cl, t, cl, base)
        a, b = shop(cl, t, wa), shop(cl, base, wb)
        same_w = a.weight_grams == b.weight_grams
        tags = ["seattle_suffix"] + (["word_order"] if (t, base) in reordered else [])
        if same_w:
            add(a, b, "match", "high", "suffix_variant",
                "Same Crimson Lotus listing stocked in the Seattle warehouse; "
                "':: Seattle Inventory' is a fulfilment label, not a product attribute.", tags)
        else:
            add(a, b, "match", "low", "suffix_variant",
                "Seattle twin of the same listing, but the Seattle variant's option text "
                "carries no parseable weight (or a different one) — likely the same cake; "
                "owner to confirm whether weight-unknown should merge.",
                tags + ["weight_unparsed_on_one_side"])

    # ------------------------------------------------------------------
    # cross_vendor_same_tea — Steepster (historical) ↔ Crimson Lotus Shopify
    # ------------------------------------------------------------------
    st_cl_high = [
        ("2000 Old Warrior", '2000 "Old Warrior" Shou / Ripe Puerh 250g Golden Melon'),
        ("2000 Old Warrior Shou", '2000 "Old Warrior" Shou / Ripe Puerh 250g Golden Melon :: Seattle Inventory'),
        ('2003 Changtai "Ji Nian / Memorial" Sheng / Raw Tuo',
         '2003 Changtai "Ji Nian / Memorial" Sheng / Raw Tuo Cha (100g) :: Seattle Inventory'),
        ("2010 Black Gold", '2010 "Black Gold" Shou / Ripe Puerh from Crimson Lotus Tea'),
        ('2010 "Black Gold"', '2010 "Black Gold" Shou / Ripe Puerh - 200g Cake :: Seattle Inventory'),
        ("2014 Huang Pian MEGA Brick", "2014 Huang Pian MEGA Brick Shou / Ripe Puerh from Crimson Lotus Tea"),
        ('2014 Yiwu "Iron Forge" Huang Pian Shou Puerh Tea',
         '2014 Yiwu "Iron Forge" Huang Pian Shou Puerh Tea (250 grams) :: Seattle Inventory'),
        ('2015 "That\'s No Moon" Shou / Ripe Puerh', '2015 "That\'s No Moon" Shou / Ripe Puerh 200g Cake'),
        ('2015 Spring "Gu Ming Xiang Bing" Yiwu - Bulang Blend',
         '2015 Spring "Gu Ming Xiang Bing" Yiwu - Bulang Blend Sheng / Raw Puerh :: Seattle Inventory'),
        ('2016 "Cloudy Days" Shou / Ripe Puerh', '2016 "Cloudy Days" Shou / Ripe Puerh 200g Cake'),
        ('2016 "Moon Princess"', '2016 "Moon Princess" Sheng / Raw Puerh from Crimson Lotus Tea'),
        ('2016 "Simple Shou"', '2016 "Simple Shou" Shou / Ripe Puerh from Crimson Lotus Tea'),
        ("2016 Beneath an Emerald Sea",
         '2016 Spring "Beneath an Emerald Sea" Sheng / Raw Puerh - 200g Cake :: Seattle Inventory'),
        ("2016 Midas Touch", 'Spring 2016 Jingmai "Midas Touch" Sheng / Raw Puerh from Crimson Lotus Tea'),
        ("2016 Jingmai LOVE", 'Spring 2016 "Jingmai LOVE" Sheng / Raw Puerh from Crimson Lotus Tea :: Seattle Inventory'),
        ('2017 "Jinggu Bàng"', 'Spring 2017 "Jinggu Bàng" Sheng / Raw Puerh from Crimson Lotus Tea'),
        ('2017 "Simple Sheng"', '2017 "Simple Sheng" Sheng / Raw Puerh from Crimson Lotus Tea'),
        ('2017 "Stargazer" Sheng / Raw Puerh Tea', '2017 "Stargazer" Sheng / Raw Puerh Tea'),
        ("2017 Midas Touch", 'Spring 2017 Jingmai "Midas Touch" Sheng / Raw Puerh from Crimson Lotus Tea'),
        ("2017 Moon Princess", '2017 "Moon Princess" Sheng / Raw Puerh from Crimson Lotus Tea'),
        ("Spring 2017 Slumbering Dragon", 'Spring 2017 "Slumbering Dragon" Sheng / Raw Puerh from Crimson Lotus Tea'),
        ('2018 "Dark Depths" Shou / Ripe Puerh', '2018 "Dark Depths" Shou / Ripe Puerh :: Seattle Inventory'),
        ('2018 "Forgotten Nebula"', '2018 "Forgotten Nebula" Sheng / Raw Puerh Tea Blend'),
        ("2018 Beneath an Emerald Sea", 'Spring 2018 "Beneath an Emerald Sea" Sheng / Raw Puerh from Crimson Lotus Tea'),
        ("2018 Road Trip", '2018 "Road Trip" Shou / Ripe Puerh 200g Brick'),
        ("2018 Slumbering Dragon", 'Spring 2018 "Slumbering Dragon" Sheng / Raw Puerh from Crimson Lotus Tea'),
        ('2018 Spring "Daydream" Sheng / Raw Puerh Tea Blend', '2018 Spring "Daydream" Sheng / Raw Puerh Tea Blend'),
        ('2018 Spring "Intrigue"', '2018 Spring "Intrigue" Sheng / Raw Puerh Tea Blend'),
        ('2018 Spring "Stone Lion"', '2018 Spring "Stone Lion" Sheng / Raw Puerh Tea Huang Pian Brick from Lao Man\'E'),
        ('2018 Spring "Wildwood" Raw Puerh', '2018 Spring "Wildwood" Sheng / Raw Puerh Tea'),
        ("2018 Spring Honeymoon Sheng Pu'erh", '2018 Spring "Honeymoon" Sheng / Raw Puerh Tea Blend'),
        ("2018 Lao Cha Tou", '2018 Jingmai "Lao Cha Tou" Shou Puerh Tea'),
        ("2019 Low Rider", '2019 Spring "Low Rider" 200g Cake - Sheng / Raw Puerh Tea'),
        ("2019 Radio KXQM", '2019 Spring "Radio KXQM" 100g Cake - Sheng / Raw Puerh Tea'),
        ('2019 Spring "Danger Zone" Sheng/Raw Puer', '2019 Spring "Danger Zone" 100g Cake - Sheng / Raw Puerh Tea'),
        ('2019 Xiaguan "Holy Flame" Baoyan Sheng', '2019 Xiaguan "Holy Flame" Baoyan Sheng / Raw Puerh Tea'),
        ("2020 Believe", '2020 "Believe" Sheng / Raw Puerh Tea'),
        ("2020 Honeybomb", '2020 "Honeybomb" Sheng / Raw Puerh Tea'),
        ("2020 The Way", '2020 "The Way" Shou / Ripe Puerh Tea'),
        ('2021 "Big Red" Dian Hong Black Tea Blend', '2021 "Big Red" Dian Hong Black Tea Blend 200g Cake'),
        ("2021 Slumbering Dragon", '2021 "Slumbering Dragon" Sheng / Raw Puerh Tea'),
        ("Elemental Puerh - 2018 Jingmai Autumn Sheng Puerh Tea Dragon Balls",
         "Elemental Puerh - 2018 Jingmai Autumn Sheng Puerh Tea Dragon Balls"),
        ("Handmade Silver Cup 70ml - Dark Thick Texture", "Handmade Silver Cup 70ml - Dark Thick Texture"),
    ]
    for st_name, cl_title in st_cl_high:
        add(steep(st_name), shop(cl, cl_title), "match", "high", "cross_vendor_same_tea",
            "Steepster (historical/reference) entry for the same Crimson Lotus pressing "
            "as the current Shopify listing — the historical→current pivot case.",
            ["steepster_to_shopify"])
    st_cl_low = [
        ("2008 Jingmai Sheng / Raw Puerh Maocha", "2008 Jingmai Loose Leaf Sheng / Raw Puerh Tea 100g",
         "Maocha ≈ loose leaf, same year/region; plausibly the same lot but naming differs."),
        ("2018 Jade Rabbit", '"Jade Rabbit" Sheng / Raw Puerh Tea Blend from Crimson Lotus Tea',
         "Shopify listing carries no year; only one Jade Rabbit cake exists, but it may be a later re-press."),
        ("Cosima", '2019 Spring "Space Girls - Cosima" 100g Cake - Sheng / Raw Puerh Tea',
         "Bare 'Cosima' on Steepster almost certainly the Space Girls Cosima cake; no year on Steepster side."),
        ("Kunlu Wild", "2024 Spring Kunlu Wild Sheng / Raw Puerh Tea - 200g Cake",
         "Steepster 'Kunlu Wild' has no year; CL pressed Kunlu Wild in several years — identity unknowable."),
    ]
    for st_name, cl_title, why in st_cl_low:
        add(steep(st_name), shop(cl, cl_title), "match" if st_name != "Kunlu Wild" else "non_match",
            "low", "cross_vendor_same_tea", why, ["steepster_to_shopify"])

    # Steepster ↔ CL / Steepster ↔ Steepster year hard negatives.
    st_year_negs = [
        (steep("2013 Lucky Cloud"), shop(cl, '2023 Jingmai "Lucky Cloud" Shou / Ripe Puerh Tea 357g Cake')),
        (steep('2014 "Cloudy Days"'), shop(cl, '2016 "Cloudy Days" Shou / Ripe Puerh 200g Cake')),
        (steep('2018 "Dark Depths" Shou / Ripe Puerh'), shop(cl, '2021 "Dark Depths" Shou / Ripe Puerh Tea')),
        (steep("2013 Xiaguan “Holy Flame” Baoyan Raw Puerh"), shop(cl, '2019 Xiaguan "Holy Flame" Baoyan Sheng / Raw Puerh Tea')),
        (steep('2019 Spring "Wildwood"'), shop(cl, '2018 Spring "Wildwood" Sheng / Raw Puerh Tea')),
        (steep("2016 Slumbering Dragon"), shop(cl, 'Spring 2017 "Slumbering Dragon" Sheng / Raw Puerh from Crimson Lotus Tea')),
        (steep("Spring 2015 Jingmai Midas Touch; Sheng"), shop(cl, 'Spring 2016 Jingmai "Midas Touch" Sheng / Raw Puerh from Crimson Lotus Tea')),
        (steep("Spring 2015 Kunlu Shan, Slumbering Dragon, Sheng Puerh"), steep("2016 Slumbering Dragon")),
        (steep("2018 Beneath an Emerald Sea"), steep("2016 Beneath an Emerald Sea")),
        (steep("2016 Whispering Sunshine"), steep("2015 Crimson Lotus Baiying 'Whispering Sunshine'")),
        (steep('2017 "Whispering Sunshine"'), steep("2016 Whispering Sunshine")),
        (steep("2016 Hidden Song"), steep("2015 Crimson Lotus Baiying 'Hidden Song'")),
        (steep("2017 Moon Princess"), steep('2016 "Moon Princess"')),
        (steep("2018 Slumbering Dragon"), steep("2021 Slumbering Dragon")),
    ]
    for a, b in st_year_negs:
        add(a, b, "non_match", "high", "hard_negative_year",
            "Same recurring Crimson Lotus recipe name, different pressing year.",
            ["steepster", "recurring_name"])

    # duplicate_listing — Steepster user-entered duplicates of the same tea.
    for a, b, conf, why in [
        ("2017 Planet Jinggu (duplicate)", '"Planet Jinggu"  2017', "high",
         "Steepster entry is literally titled '(duplicate)' of the 2017 Planet Jinggu page."),
        ("2010 Black Gold", '2010 "Black Gold"', "high", "Two Steepster pages for the same 2010 Black Gold shou."),
        ('2013 Jingmai "Lucky Cloud"', "2013 Lucky Cloud", "high", "Two Steepster pages for the 2013 Lucky Cloud."),
        ("2000 Old Warrior", "2000 Old Warrior Shou", "high", "Two Steepster pages for the 2000 Old Warrior shou."),
        ('"Planet Bangdong" 2017', '"Planet Kunlu" 2017', "high",
         "Sibling 'Planet' series cakes from different mountains — NOT a duplicate (control)."),
    ]:
        add(steep(a), steep(b), "non_match" if "control" in why else "match", conf,
            "duplicate_listing" if "control" not in why else "hard_negative_name_variant", why,
            ["steepster", "same_source"])

    # ------------------------------------------------------------------
    # cross_vendor_same_tea — YS .us ↔ YS .com storefronts (same catalog)
    # ------------------------------------------------------------------
    both = sorted(set(CAT.shop[us]) & set(CAT.shop[com]), key=_h)
    easy = 0
    for t in both:
        a_it, b_it = CAT.item(us, t), CAT.item(com, t)
        if not is_tea(a_it) or a_it.producer != b_it.producer or a_it.year != b_it.year:
            continue
        wa, wb = common_weight(us, t, com, t)
        add(shop(us, t, wa), shop(com, t, wb), "match", "high", "cross_vendor_same_tea",
            "Identical listing on the YS .us and .com storefronts (same catalog, two vendor rows).",
            ["ys_storefronts", "identical_title"])
        easy += 1
        if easy >= 16:
            break
    # Attribute drift between storefronts on the same listing.
    drift = [
        ('2024 Yunnan Sourcing "Naka Beauty" Old Arbor Raw Pu-erh Tea Cake', 250, 250, "match", "low",
         ("Title says 2024 on both storefronts; .us harvest tag says 2022 — tag noise or a "
          "2022-material 2024 pressing. Owner to confirm."), ["attribute_noise_year"]),
        ("2012 Rice Scent Herb & Ripe Pu-erh Tea Mini Tuo", 100, 100, "match", "high",
         ("Same listing; producer tag differs by storefront ('Yunnan Sourcing USA' vs "
          "'Yunnan Sourcing Tea Shop') — producer blocking will veto."), ["producer_tag_drift"]),
        ("Cream Soda Dan Cong Oolong Tea", 25, 25, "non_match", "low",
         ("Evergreen loose-leaf listing; storefront harvest tags disagree (2025 vs 2026) — by §8 "
          "year is identity, so different harvest lots."), ["year_tag_drift"]),
        ("Competition Dragon Well Tea From Zhejiang * Long Jing Tea", 25, 25, "non_match", "low",
         "Storefront harvest tags disagree (2026 vs 2025) — different harvest lots by §8.", ["year_tag_drift"]),
        ("Light Roast \"Gao Cong Shui Xian\" Rock Oolong Tea", 25, 25, "non_match", "low",
         "Storefront harvest tags disagree (2023 vs 2024).", ["year_tag_drift"]),
        ("Mei Zhan Varietal Black Tea from Tong Mu Village", 50, 50, "non_match", "low",
         "Storefront harvest tags disagree (2025 vs 2024).", ["year_tag_drift"]),
        ("Hong Mao Feng Jasmine Black Tea", 100, 100, "match", "low",
         "Same listing; only .com carries a harvest tag (2025). One-sided year.", []),
        ('Tong Mu Village "Dan Ya" Jin Jun Mei Black Tea', 25, 25, "match", "low",
         "Same listing; only .us carries a harvest tag (2025). One-sided year.", []),
        ('High Mountain "Lao Cong Huang Zhi Xiang" Dan Cong Oolong Tea', 30, 30, "match", "low",
         "Same listing; only .com carries a harvest tag (2024). One-sided year.", []),
        ("Jinggu Wild Tree Purple Black Tea", 25, 25, "match", "low",
         "Same listing; only .com carries a harvest tag. One-sided year.", []),
        ("Further Steps Tea Sampler", NONE, NONE, "non_match", "low",
         "Sampler re-composed per season; harvest tags 2024 vs 2025.", ["year_tag_drift", "sampler"]),
    ]
    for t, wa, wb, label, conf, why, tags in drift:
        add(shop(us, t, wa), shop(com, t, wb), label, conf, "cross_vendor_same_tea", why,
            ["ys_storefronts", "identical_title", *tags])
    add(shop(us, '2025 Yunnan Sourcing "Cha Qi" Raw Pu-erh Tea Cake in Gift Box', 357),
        shop(com, '2025 Yunnan Sourcing "Cha Qi" Raw Pu-erh Tea Cake', 357), "match", "low",
        "duplicate_listing", "Same 2025 Cha Qi cake; 'in Gift Box' is packaging. Owner: does packaging "
        "split a product?", ["packaging"])
    add(shop(us, '2023 Xiaguan "Xiao Fa" Ripe Pu-erh Tea Tuo', 100),
        shop(us, '2023 Xiaguan "Xiao Fa" Ripe Pu-erh Tea Tuo in Box', 100), "non_match", "low",
        "duplicate_listing", "Loose tuo vs boxed tuo of the same 2023 Xiao Fa — separate SKUs; "
        "packaging-only difference. Owner to rule.", ["packaging"])
    for v, dtitles in sorted(CAT.dups.items()):
        for t in sorted(dtitles):
            a, b = shop(v, t), shop(v, t, dup=1)
            add(a, b, "match", "low", "duplicate_listing",
                "The same title is listed twice on one storefront (two product ids); likely a "
                "relisting of the same tea. Owner: merge, or keep separate SKUs?", ["same_source"])
    add(shop(w2t, "Basics Puer Tea Sample Set", 400, dup=1), shop(w2t, "Basics Ripe Puer Tea Sample Set", 400),
        "non_match", "high", "hard_negative_sheng_shou",
        "Raw-leaning 'Basics' set vs the Ripe set — different contents, same weight.", ["sampler"])

    # ------------------------------------------------------------------
    # hard_negative_sheng_shou (blocking passes: same producer/year/weight)
    # ------------------------------------------------------------------
    sheng_shou = [
        (w2t, "2016 ChocoBrick Raw Puer", w2t, "2016 ChocoBrick Ripe Puer", AUTO, AUTO),
        (w2t, "2017 Flap Jacks - Raw Puer", w2t, "2017 Flap Jacks - Ripe Puer", NONE, NONE),
        (w2t, "2018 Flapjacks Raw Puer", w2t, "2018 Flapjacks Ripe Puer", AUTO, AUTO),
        (w2t, "Decaf Raw Puer", w2t, "Decaf Shou Puer", AUTO, AUTO),
        (w2t, "Tea Terroir Raw Puer", w2t, "Tea Terroir Ripe Puer", AUTO, AUTO),
        (us, '2023 Yunnan Sourcing "Lao Man\'e Village" Raw Pu-erh Tea Cake', us,
         '2023 Yunnan Sourcing "Lao Man\'e Village" Ripe Pu-erh Tea Cake', 250, 250),
        (us, '2024 Yunnan Sourcing "Impression" Raw Pu-erh Tea Cake', us,
         '2024 Yunnan Sourcing "Impression" Ripe Pu-erh Tea Cake', 357, 357),
        (us, '2022 Zu Xiang "Wu Liang Chuan Qi 888" Certified Organic Raw Pu-erh Tea', us,
         '2022 Zu Xiang "Wu Liang Chuan Qi 888" Certified Organic Ripe Pu-erh Tea', 888, 888),
        (us, '2024 Xiaguan "Raw Mini Tuo Cha" Pu-erh Tea', us, '2024 Xiaguan "Ripe Mini Tuo Cha" Pu-erh Tea', 200, 200),
        (us, 'Jinggu "Cha Gao" Instant Raw Pu-erh Tea Resin', us, 'Jinggu "Cha Gao" Instant Ripe Pu-erh Tea Resin', 25, 25),
        (us, '2024 Yunnan Sourcing "Bang Wai Mountain" Raw Pu-erh Tea Cake', com,
         '2024 Yunnan Sourcing "Bang Wai Mountain" Ripe Pu-erh Tea Cake', 25, 25),
        (us, 'Aged "Cha Gao" Instant Raw Pu-erh Tea Resin', us, 'Jinggu "Cha Gao" Instant Ripe Pu-erh Tea Resin', 25, 25),
        (us, '2024 Yunnan Sourcing "Green Mark Dragon" Ripe Raw Pu-erh Tea Cake', us,
         '2024 Yunnan Sourcing "Subduer of Dragons" Ripe Raw Pu-erh Tea Cake', 25, 25),
        (cl, '2016 "Simple Shou" Shou / Ripe Puerh from Crimson Lotus Tea', cl,
         '2017 "Simple Sheng" Sheng / Raw Puerh from Crimson Lotus Tea', 50, 50),
        (cl, "2021 Pasha Shan Shou / Ripe Puerh Tea", cl, '2021 Pasha Shan "Lao Cha Tou" Shou Puerh Tea', 200, 200),
    ]
    for v1, t1, v2, t2, w1, w2 in sheng_shou:
        why = ("Raw (sheng) vs ripe (shou) pu-erh — different products despite near-identical titles."
               if "Lao Cha Tou" not in t2 and "Subduer" not in t2 else
               "Both ripe, but different products (cake vs lao cha tou nuggets / different blends).")
        add(shop(v1, t1, w1), shop(v2, t2, w2), "non_match", "high", "hard_negative_sheng_shou", why)

    # hard_negative_tea_type (black / white / raw from the same garden).
    tea_type = [
        (us, '2023 Yunnan Sourcing "Suan Zao Shu" Old Arbor Black Tea Cake', '2023 Yunnan Sourcing "Suan Zao Shu" Old Arbor Raw Pu-erh Tea Cake', 250, 250),
        (us, '2024 Yunnan Sourcing "Suan Zao Shu" Old Arbor Black Tea Cake', '2024 Yunnan Sourcing "Suan Zao Shu" Old Arbor White Tea Cake', 250, 250),
        (us, '2024 Yunnan Sourcing "Suan Zao Shu" Old Arbor Raw Pu-erh Tea Cake', '2024 Yunnan Sourcing "Suan Zao Shu" Old Arbor White Tea Cake', 250, 250),
        (us, '2025 Yunnan Sourcing "Suan Zao Shu" Old Arbor Raw Pu-erh Tea Cake', '2025 Yunnan Sourcing "Suan Zao Shu" Old Arbor White Tea Cake', 250, 250),
        (us, '2025 Yunnan Sourcing "Ku Zhu Shan Hong" Black Tea Cake', '2025 Yunnan Sourcing "Ku Zhu Shan" Raw Pu-erh Tea Cake', 250, 250),
        (us, '2024 Yunnan Sourcing "Ku Zhu Shan Hong" Black Tea Cake', '2024 Yunnan Sourcing "Ku Zhu Shan" Raw Pu-erh Tea Cake', 250, 250),
        (us, '2025 Yunnan Sourcing "Meng Zhu Da Shan" Old Arbor Raw Pu-erh Tea Cake', '2025 Yunnan Sourcing "Meng Zhu Da Shan" Old Arbor Sun-Dried Black Tea Cake', 25, 25),
        (us, '2024 Yunnan Sourcing "Jiu Tai Po Bai Cha" Old Arbor White Tea Cake', '2024 Yunnan Sourcing "Jiu Tai Po" Old Arbor Raw Pu-erh Tea Cake', 25, 25),
        (us, '2025 Yunnan Sourcing "Jiu Tai Po Bai Cha" Old Arbor White Tea Cake', '2025 Yunnan Sourcing "Jiu Tai Po" Old Arbor Raw Pu-erh Tea Cake', 250, 250),
        (us, '2023 Yunnan Sourcing "Purple White Tea" of Yong De Tea Cake', '2023 Yunnan Sourcing "Wild Purple Black" of Yong De Tea Cake', 250, 250),
        (us, '2023 Yunnan Sourcing "Meng Song Village" White Tea Cake', '2023 Yunnan Sourcing "Meng Song" Ripe Pu-erh Tea Cake', 250, 250),
        (us, '2025 Yunnan Sourcing "Green Label" Wild Arbor Raw Pu-erh Tea Mini Cakes', '2025 Yunnan Sourcing "Red Label" Wild Arbor Black Tea Mini Cakes', 100, 100),
        (us, '2025 Yunnan Sourcing "Red Label" Wild Arbor Black Tea Mini Cakes', '2025 Yunnan Sourcing "Silver Label" Wild Arbor White Tea Mini Cakes', 100, 100),
        (us, 'Yunnan "Qing Xin" Taiwan Varietal Jade Oolong Tea', 'Yunnan "Qing Xin" Taiwan Varietal Red Oolong Tea', 50, 50),
        (w2t, "2021 Yesheng Gushu Baicha", "2021 Yesheng Gushu Hongcha", 200, 200),
        (w2t, "2022 Yesheng Gushu Baicha", "2022 Yesheng Gushu Hongcha", 200, 200),
        (w2t, "2023 Yesheng Gushu Baicha", "2023 Yesheng Gushu Hongcha", 200, 200),
        (w2t, "2025 Yesheng Gushu Baicha", "2025 Yesheng Gushu Hongcha", 200, 200),
        (w2t, "2019 Arbor Red", "2019 Arbor White", 200, 200),
        (w2t, "2020 Arbor Red", "2020 Arbor White", 200, 200),
        (w2t, "2016 ChocoBrick Raw Puer", "2016 ChocoBrick White Tea", 100, 100),
    ]
    for v, t1, t2, w1, w2 in tea_type:
        add(shop(v, t1, w1), shop(v, t2, w2), "non_match", "high", "hard_negative_tea_type",
            "Same garden / series, different processing (black vs white vs raw vs oolong style).")

    # ------------------------------------------------------------------
    # hard_negative_recipe_code — numeric codes (recipe / series / model)
    # ------------------------------------------------------------------
    codes_real = [
        (w2t, "No.15 Rougui", "No.2 Rougui", 25, "rock_oolong_series"),
        (w2t, "No.26 Rougui", "No.6 Rougui", 25, "rock_oolong_series"),
        (w2t, "No.8 Rougui", "No.6 Rougui", 25, "rock_oolong_series"),
        (w2t, "No.17 Milan Dancong", "No.24 Milan Dancong", 25, "series_number"),
        (w2t, "Traditional Tie Guan Yin Type One", "Traditional Tie Guan Yin Type Three", 8, "series_number"),
        (w2t, "Traditional Tie Guan Yin Type Three", "Traditional Tie Guan Yin Type Four", 8, "series_number"),
        (w2t, "Tie Guan Yin Type A", "Tie Guan Yin Type C", 8, "series_number"),
        (w2t, "2017 The Treachery of Story Telling Pt.1", "2017 The Treachery of Story Telling Pt.2", 200, "series_number"),
        (us, '2003 CNNP "7262" Ripe Pu-erh Tea Cake', "2003 CNNP 7581 Aged Ripe Pu-erh Tea Brick", 25, "recipe"),
        (us, "Bonston BP-12 Automatic Tea Brewer", "Bonston BP-13 Automatic Tea Brewer", NONE, "teaware_model"),
        (us, 'SAMA "MC-11" Insulated Thermos with Cup', 'SAMA "MC09" Insulated Thermal Flask with Cup for Brewing Tea', NONE, "teaware_model"),
        (com, 'Jian Shui Clay "Lotus ZA06" Teapot by Su Mo', 'Jian Shui Clay "Lotus ZH03" Teapot by Su Mo', NONE, "teaware_model"),
        (com, 'Jian Shui Clay "Lotus ZH03" Teapot by Su Mo', 'Jian Shui Clay "Lotus ZH95" Teapot by Su Mo', NONE, "teaware_model"),
        (com, 'Jian Shui Clay "Jiao Ni 66" Teapot by Wang Yan Ping', 'Jian Shui Clay "Jiao Ni 71" Teapot by Wang Yan Ping', NONE, "teaware_model"),
        (cl, 'Huaning "Super Glaze" Teacup #03 - 70ml', 'Huaning "Super Glaze" Teacup #04 - 70ml', NONE, "teaware_model"),
        (cl, 'Huaning "Super Glaze" Teacup #04 - 70ml', 'Huaning "Super Glaze" Teacup #05 - 70ml', NONE, "teaware_model"),
        (cl, 'Vintage Wood Fired Jianzhan Teacup #16 80ml "Oil Drop"', 'Vintage Wood Fired Jianzhan Teacup #20 80ml "Oil Drop"', NONE, "teaware_model"),
        (cl, "Yunnan Dragon Kiln Wood Fired Tea Cup - Grade 2", "Yunnan Dragon Kiln Wood Fired Tea Cup - Grade 3", NONE, "grade"),
        (com, "Yunnan Sourcing Brand Ripe Pu-erh Tea Sampler for 2023 - Part 1",
         "Yunnan Sourcing Brand Ripe Pu-erh Tea Sampler for 2023 - Part 2", NONE, "series_number"),
        (com, "Yunnan Sourcing Brand Ripe Pu-erh Tea Sampler for 2024 - Part 2",
         "Yunnan Sourcing Brand Ripe Pu-erh Tea Sampler for 2024 - Part 3", NONE, "series_number"),
    ]
    for v, t1, t2, w, tag in codes_real:
        add(shop(v, t1, w), shop(v, t2, w), "non_match", "high", "hard_negative_recipe_code",
            "Only a numeric code / series number differs — a distinct product (recipe, part, or model).",
            [tag])
    # Derived recipe-code swaps: the classic Dayi 7262 vs 7572 case. Swapped codes are
    # attested elsewhere in the catalog (7262 CNNP, 7542 Dayi raw, 7540 Liming, 2503 Three Cranes).
    for base_title, v, old, new in [
        ('2023 Menghai "7572" Ripe Pu-erh Tea Cake', us, "7572", "7262"),
        ('2024 Menghai "7572" Ripe Pu-erh Tea Cake', us, "7572", "7562"),
        ('2022 Menghai "7572" Ripe Pu-erh Tea Cake', us, "7572", "7452"),
        ("2012 Menghai Tea Factory 7542 Recipe Raw Pu-erh Tea Cake", com, "7542", "7540"),
        ("2005 Liming 7540 Raw Pu-erh Tea Cake", us, "7540", "7542"),
        ('2020 Three Cranes "1815 Recipe" Liu Bao Tea Brick', us, "1815", "2503"),
        ('2003 Tian Di Ren “7572” Ripe Pu-erh Tea Cake', us, "7572", "7262"),
    ]:
        base = shop(v, base_title)
        add(base, derive(base, base_title.replace(old, new), "recipe_code_swap", new),
            "non_match", "high", "hard_negative_recipe_code",
            f"Recipe code {old} vs {new}: same factory/year/weight, different blend. Embeddings "
            "collapse these; the attribute guard must veto.", ["recipe"])
    # Year differs on a shared recipe code (real pairs; blocking catches these).
    for v1, t1, v2, t2 in [
        (us, '2022 Menghai "7572" Ripe Pu-erh Tea Cake', us, '2023 Menghai "7572" Ripe Pu-erh Tea Cake'),
        (us, '2023 Menghai "7572" Ripe Pu-erh Tea Cake', us, '2024 Menghai "7572" Ripe Pu-erh Tea Cake'),
        (us, '1998 CNNP "7262 Yellow Mark" Ripe Pu-erh Tea Cake', us, '2003 CNNP "7262" Ripe Pu-erh Tea Cake'),
        (us, "2002 Menghai 7562 Ripe Pu-erh Tea Brick", us, '2000 "7562 Recipe" Aged Ripe Pu-erh Tea Brick'),
        (us, "2019 Menghai DaYi V93 Premium Ripe Pu-erh Tea Tuo", us, "2022 Menghai DaYi V93 Premium Ripe Pu-erh Tea Tuo"),
        (us, "2007 Menghai DaYi V93 Premium Ripe Pu-erh Tea Tuo", com, "2007 Menghai Wei Zui Yan Ripe Pu-erh Tea Cake"),
    ]:
        wa, wb = common_weight(v1, t1, v2, t2)
        add(shop(v1, t1, wa), shop(v2, t2, wb), "non_match", "high", "hard_negative_year",
            "Same factory recipe/series, different production year (or different product).", ["recipe"])

    # ------------------------------------------------------------------
    # hard_negative_year — year not at title start (V1 parser returns None on both
    # sides, so blocking passes) + auto-mined recurring-name series.
    # ------------------------------------------------------------------
    year_unparsed = [
        (cl, 'Spring 2017 "Slumbering Dragon" Sheng / Raw Puerh from Crimson Lotus Tea',
         'Spring 2018 "Slumbering Dragon" Sheng / Raw Puerh from Crimson Lotus Tea'),
        (cl, 'Spring 2018 "Slumbering Dragon" Sheng / Raw Puerh from Crimson Lotus Tea',
         'Spring 2019 "Slumbering Dragon" Sheng / Raw Puerh from Crimson Lotus Tea'),
        (cl, 'Spring 2016 Jingmai "Midas Touch" Sheng / Raw Puerh from Crimson Lotus Tea',
         'Spring 2017 Jingmai "Midas Touch" Sheng / Raw Puerh from Crimson Lotus Tea'),
        (cl, 'Spring 2016 "Jingmai LOVE" Sheng / Raw Puerh from Crimson Lotus Tea :: Seattle Inventory',
         'Spring 2018 "Jingmai LOVE" Sheng / Raw Puerh from Crimson Lotus Tea'),
        (cl, "Elemental Puerh - 2019 Bulang Sheng Puerh Tea Dragon Balls",
         "Elemental Puerh - 2020 Bulang Sheng Puerh Tea Dragon Balls"),
        (cl, "Elemental Puerh - 2019 Jingmai Old Tree Sheng Puerh Tea Dragon Balls",
         "Elemental Puerh - 2020 Jingmai Old Tree Sheng Puerh Tea Dragon Balls"),
        (cl, "Elemental Puerh - 2008 Jingmai Old Tree Sheng Puerh Tea Dragon Balls",
         "Elemental Puerh - 2019 Jingmai Old Tree Sheng Puerh Tea Dragon Balls"),
        (cl, "Puerhiodic Table of the Elements 2019 - 6x Sheng Puerh Tea Dragon Balls",
         "Puerhiodic Table of the Elements 2020 - 6x Sheng Puerh Tea Dragon Balls"),
        (com, "Yunnan Sourcing Brand Ripe Pu-erh Tea Sampler for 2023 - Part 1",
         "Yunnan Sourcing Brand Ripe Pu-erh Tea Sampler for 2024 - Part 1"),
        (com, "Yunnan Sourcing Brand Ripe Pu-erh Tea Sampler for 2023 - Part 3",
         "Yunnan Sourcing Brand Ripe Pu-erh Tea Sampler for 2024 - Part 3"),
    ]
    for v, t1, t2 in year_unparsed:
        wa, wb = common_weight(v, t1, v, t2)
        add(shop(v, t1, wa), shop(v, t2, wb), "non_match", "high", "hard_negative_year",
            "Different pressing year, but the year is not at title start so the V1 regex "
            "(^(19|20)\\d\\d) parses None on both sides and year blocking does not fire.",
            ["year_not_parsed"])
    cl_series = [
        ('2016 "Moon Princess" Sheng / Raw Puerh from Crimson Lotus Tea', '2017 "Moon Princess" Sheng / Raw Puerh from Crimson Lotus Tea'),
        ('2017 "Moon Princess" Sheng / Raw Puerh from Crimson Lotus Tea', '2019 "Moon Princess" Sheng / Raw Puerh'),
        ('2020 "Honeybomb" Sheng / Raw Puerh Tea', '2022 "Honeybomb" Sheng / Raw Puerh Tea'),
        ('2022 "Honeybomb" Sheng / Raw Puerh Tea', '2024 "Honeybomb" Sheng / Raw Puerh Tea'),
        ('2020 "Slumbering Dragon" Sheng / Raw Puerh Tea', '2021 "Slumbering Dragon" Sheng / Raw Puerh Tea'),
        ("2022 Spring Kunlu Xiao Qiao Mu 200g Cake - Sheng / Raw Puerh Tea", "2023 Kunlu Xiao Qiao Mu Sheng / Raw Puerh Tea - 200g Cake"),
        ("2023 Kunlu Xiao Qiao Mu Sheng / Raw Puerh Tea - 200g Cake", "2024 Spring Kunlu Xiao Qiao Mu Sheng / Raw Puerh Tea - 200g Cake"),
        ('2023 Lincang "Section A" Gushu Sheng / Raw Puerh Tea - 200g Cake', '2024 Spring Lincang "Section A" Gushu Sheng / Raw Puerh Tea - 200g Cake'),
        ("2022 Spring Hekai Old Tree 200g Cake - Sheng / Raw Puerh Tea", "2023 Hekai Old Tree Sheng / Raw Puerh Tea - 200g Cake"),
        ("2023 Jingmai Old Tree Sheng / Raw Puerh Tea - 200g Cake", "2024 Spring Jingmai Old Tree - Sheng / Raw Puerh Tea - 200g Cake"),
        ('2023 Bai Ying Shan "Whispering Sunshine" Sheng / Raw Puerh Tea - 200g Cake', '2024 Spring Bai Ying Shan "Whispering Sunshine" Sheng / Raw Puerh Tea - 200g Cake'),
        ("2022 Spring Bulang 200g Cake - Sheng / Raw Puerh Tea", "2025 Spring Bulang Sheng / Raw Puerh Tea - 200g Cake"),
    ]
    for t1, t2 in cl_series:
        wa, wb = common_weight(cl, t1, cl, t2)
        add(shop(cl, t1, wa), shop(cl, t2, wb), "non_match", "high", "hard_negative_year",
            "Recurring Crimson Lotus recipe/garden, different pressing year.", ["recurring_name"])
    for v, cap in ((w2t, 10), (us, 6)):
        groups: dict[str, list[str]] = defaultdict(list)
        for t, it in CAT.shop[v].items():
            if it.year is not None and YEAR_PREFIX.match(t) and is_tea(it):
                groups[strip_year(t)].append(t)
        cands = []
        for base, ts in groups.items():
            ts = sorted(ts)
            for a, b in itertools.pairwise(ts):
                if CAT.item(v, a).year != CAT.item(v, b).year:
                    cands.append((a, b))
        n = 0
        for a, b in sorted(cands, key=lambda ab: _h(ab[0] + "|" + ab[1])):
            wa, wb = common_weight(v, a, v, b)
            if add(shop(v, a, wa), shop(v, b, wb), "non_match", "high", "hard_negative_year",
                   "Same recurring product name, consecutive pressing years.", ["recurring_name"]):
                n += 1
            if n >= cap:
                break

    # ------------------------------------------------------------------
    # hard_negative_weight — same tea, different variant weight (§8 identity)
    # ------------------------------------------------------------------
    for v, cap in ((w2t, 6), (us, 6), (cl, 4)):
        multi = sorted(
            (t for t, it in CAT.shop[v].items()
             if is_tea(it) and len({w for w in it.weights if w is not None}) >= 2),
            key=_h,
        )
        for t in multi[:cap]:
            ws = sorted({w for w in CAT.item(v, t).weights if w is not None})
            add(shop(v, t, ws[-1]), shop(v, t, ws[0]), "non_match", "high", "hard_negative_weight",
                f"Same listing, {ws[-1]} g vs {ws[0]} g variant — weight is part of the §8 product "
                "identity (policy label; owner may revisit).", ["same_listing_variant"])
    minis = []
    for t in CAT.shop[w2t]:
        for suf in (" Mini", " Minis"):
            if t.endswith(suf) and t[: -len(suf)] in CAT.shop[w2t]:
                minis.append((t[: -len(suf)], t))
    for full, mini in sorted(minis, key=lambda p: _h(p[1]))[:10]:
        add(shop(w2t, full), shop(w2t, mini), "non_match", "high", "hard_negative_weight",
            "Full cake vs the '~7 g Mini(s)' companion SKU of the same tea — different product.",
            ["sibling_mini"])
    add(shop(w2t, "Snowflake Dancong"), shop(w2t, "Snowflake Dancong Coins", NONE), "non_match", "high",
        "hard_negative_weight", "Loose Snowflake Dancong vs the pressed 'Coins' format.", ["format"])

    # ------------------------------------------------------------------
    # hard_negative_name_variant — sibling products sharing a title template
    # ------------------------------------------------------------------
    name_variants = [
        (w2t, "2019 Natural Redhead", "2019 Natural Redhead Reserve", 200),
        (w2t, "2019 Hot Brandy", "2019 Hot Brandy Reserva", 200),
        (w2t, "2019 Nightfall", "2019 Nightfall Bamboo", 200),
        (w2t, "2019 Tiltshift", "2019 Tiltshift Bamboo", 200),
        (w2t, "2021 Chapo", "2021 Chapo Mid", 500),
        (w2t, "2024 Chapo", "2024 Chapo Mid", 500),
        (w2t, "2023 Milan Maocha Brick", "2023 Milan Maocha Top Shelf", 200),
        (w2t, "Dahongpao", "Dahongpao OLD", 25),
        (w2t, "Daily Duck Shit Dancong", "Red Duck Shit Dancong", 25),
        (w2t, "Tie Guan Yin Long Haul", "Tie Guan Yin Old Style", 8),
        (w2t, "2019 Lu'An Gongjian", "2019 Lu'An Tegong", 250),
        (w2t, "2021 Raw Autumn Liubao", "2021 Raw Spring Liubao", 25),
        (w2t, "2013 Gongmei", "2013 Shoumei", 360),
        (us, '2025 Yunnan Sourcing "Peerless Gold Label" Ripe Pu-erh Tea Cake', '2025 Yunnan Sourcing "Peerless Red Label" Ripe Pu-erh Tea Cake', 357),
        (us, '2024 Yunnan Sourcing "Impression" Raw Pu-erh Tea Cake', '2024 Yunnan Sourcing "Lincang Impression" Raw Pu-erh Tea Cake', 357),
        (us, '2022 Yunnan Sourcing "Gold Impression" Raw Pu-erh Tea Cake', '2022 Yunnan Sourcing "Silver Impression" Raw Pu-erh Tea Cake', 357),
        (us, '2025 Yunnan Sourcing "Naka Snake" Old Arbor Raw Pu-erh Tea Cake', '2025 Yunnan Sourcing "Ba Da Snake" Raw Pu-erh Tea Cake', 250),
        (us, '2023 Yunnan Sourcing "Lao Man\'e Old Tree" Ripe Pu-erh Tea Cake', '2023 Yunnan Sourcing "Lao Man\'e Village" Ripe Pu-erh Tea Cake', 25),
        (us, '2023 Yunnan Sourcing "Da Qing Gu Shu" Raw Pu-erh Tea Cake', '2023 Yunnan Sourcing "Da Qing Zhai" Old Arbor Raw Pu-erh Tea Cake', 25),
        (us, '2023 Yunnan Sourcing "Jiu Tai Po" Old Arbor Raw Pu-erh Tea Cake', '2023 Yunnan Sourcing "Autumn Jiu Tai Po" Old Arbor Raw Pu-erh Tea Cake', 25),
        (us, '2024 Yunnan Sourcing "Suan Zao Shu" Old Arbor Raw Pu-erh Tea Cake', '2024 Yunnan Sourcing "Autumn Suan Zao Shu" Old Arbor Raw Pu-erh Tea Cake', 25),
        (us, '2024 Yunnan Sourcing "Gold Label" Ripe Pu-erh Tea Cake', '2024 Yunnan Sourcing "Blue Label" Ripe Pu-erh Tea Cake', 25),
        (us, '2023 Yunnan Sourcing "Mengku Gu Shu Bai Cha" White Tea Cake', '2023 Yunnan Sourcing "Mengku Teng Tiao" Ripe Pu-erh Tea Cake', 357),
        (com, '2024 Yunnan Sourcing "Ge Deng" Ancient Arbor Raw Pu-erh Tea Cake', '2024 Yunnan Sourcing "Autumn Ge Deng" Ancient Arbor Raw Pu-erh Tea Cake', 200),
        (com, '2024 Yunnan Sourcing "Zhang Jia Wan" Ancient Arbor Raw Pu-erh Tea Cake', '2024 Yunnan Sourcing "Autumn Zhang Jia Wan" Ancient Arbor Raw Pu-erh Tea Cake', 200),
        (com, '2024 Yunnan Sourcing "Yi Shan Mo" Yi Wu Ancient Arbor Raw Pu-erh Tea Cake', '2024 Yunnan Sourcing "Autumn Yi Shan Mo" Ancient Arbor Raw Pu-erh Tea Cake', 200),
        (com, '2024 Yunnan Sourcing "Autumn Ge Deng" Ancient Arbor Raw Pu-erh Tea Cake', '2024 Yunnan Sourcing "Autumn Mang Zhi" Ancient Arbor Raw Pu-erh Tea Cake', 200),
        (com, '2025 Yunnan Sourcing "Luo Shui Dong" Ancient Arbor Raw Pu-erh Tea Cake', '2025 Yunnan Sourcing "Wa Long Village" Ancient Arbor Raw Pu-erh Tea Cake', 200),
        (com, 'Jian Shui Clay "She De KA39" Teapot by Kong Ye', 'Jian Shui Clay "She De" Teapot by Kong Ye', NONE),
        (cl, '2021 "Big Red" Dian Hong Black Tea Blend 200g Cake', '2021 "Roam" Dian Hong Black Tea Blend 200g Cake', 200),
        (cl, '2021 "Intergalactic" Dian Hong Black Tea Blend 200g Cake', '2021 "Roam" Dian Hong Black Tea Blend 200g Cake', 200),
        (cl, '2020 "Baby Dragon" Sheng / Raw Puerh Tea', '2020 "Slumbering Dragon" Sheng / Raw Puerh Tea', 200),
        (cl, '2021 "Sunlicker" Sheng / Raw Puerh Tea', '2021 "Love Potion" Sheng / Raw Puerh Tea', 200),
        (cl, '2021 "Altered State" Sheng / Raw Puerh Tea', '2021 "The Sprawl" Sheng / Raw Puerh Tea', 200),
        (cl, '2021 "Vibe" Sheng / Raw Puerh Tea', '2021 "Whispering Sunshine" Sheng / Raw Puerh Tea', 200),
        (cl, '2019 Spring "Space Girls - Cosima" 100g Cake - Sheng / Raw Puerh Tea', '2019 Spring "Space Girls - Feist" 100g Cake - Sheng / Raw Puerh Tea', 100),
        (cl, '2019 Spring "Danger Zone" 100g Cake - Sheng / Raw Puerh Tea', '2019 "Fall into the Danger Zone" 100g Cake - Autumn Sheng / Raw Puerh Tea', 100),
        (cl, "Mini Bangdong Cake (50g) Sheng / Raw Puerh Tea", "Mini Hekai Cake (50g) Sheng / Raw Puerh Tea", 50),
        (cl, "Mini Hekai Cake (50g) Sheng / Raw Puerh Tea", "Mini Mengku Cake (50g) Sheng / Raw Puerh Tea", 50),
        (cl, "Elemental Puerh - 2018 Lincang Wild Sheng Puerh Tea Dragon Balls", "Elemental Puerh - 2018 Manzhuan Old Tree Sheng Puerh Tea Dragon Balls", 48),
        (cl, "Shou Puerh Tea Super Sample Pack - Level ONE!", "Shou Puerh Tea Super Sample Pack - Level TWO!", NONE),
        (cl, "2024 Spring Kunlu Wild Sheng / Raw Puerh Tea - 200g Cake", "2024 Spring Kunlu Xiao Qiao Mu Sheng / Raw Puerh Tea - 200g Cake", 200),
        (cl, '2024 Yiwu "Shousparilla" Lao Cha Tou Shou Puerh Tea', '2024 Yiwu "Shousparilla" Shou / Ripe Puerh Tea - 200g Cake', 200),
        (cl, "Jianshui White Lotus Teapot - Xishi Style", "Jianshui Black Lotus Teapot - Xishi Style", NONE),
    ]
    for v, t1, t2, w in name_variants:
        add(shop(v, t1, w), shop(v, t2, w), "non_match", "high", "hard_negative_name_variant",
            "Sibling product sharing the vendor's title template (same producer/year/weight); "
            "differs only in the distinguishing name token.")
    add(shop(w2t, "DHP no.3"), shop(w2t, "Dahongpao"), "non_match", "low", "hard_negative_name_variant",
        "'DHP no.3' is a separate numbered Dahongpao listing; abbreviation makes it look like an alias.",
        ["abbreviation"])

    # ------------------------------------------------------------------
    # cross_script — CJK (token attested in the product's own body_html) + romanization
    # ------------------------------------------------------------------
    cjk = [
        (us, '2025 Yunnan Sourcing "Bing Dao Lao Zhai" Raw Pu-erh Tea Cake', "冰岛老寨", "2025 冰岛老寨 生普洱 茶饼"),
        (us, '2025 Yunnan Sourcing "Mo Lie Shan" Raw Pu-erh Tea Cake', "磨烈山", "2025 磨烈山 生普洱"),
        (us, '2025 Yunnan Sourcing "Ba Wang" Ripe Pu-erh Tea Cake', "霸王", "2025 霸王 熟普洱 茶饼"),
        (us, '2010 Yunnan Sourcing "Ding Jia Zhai" Autumn Harvest Yi Wu Pu-erh Tea Cake', "丁家寨", "2010 丁家寨 谷花茶 生饼"),
        (us, '2003 Tian Di Ren “7572” Ripe Pu-erh Tea Cake', "天地人", "2003 天地人 7572 七子饼"),
        (us, '2025 Yunnan Sourcing "Meng Zhu Da Shan" Old Arbor Raw Pu-erh Tea Cake', "勐主大山", "2025 勐主大山 古树 生饼"),
        (us, '2025 Yunnan Sourcing "San Ke Shu" Old Arbor Raw Pu-erh Tea Cake', "三棵树", "2025 三棵树 古树 生普洱"),
        (us, '2025 Yunnan Sourcing "Xi Niu Tang Pasha" Raw Pu-erh Tea Cake', "犀牛塘", "2025 帕沙山 犀牛塘 生普洱"),
        (us, '2025 Yunnan Sourcing "Lotus River Dream" Ripe Pu-erh Tea Cake', "莲河梦", "2025 莲河梦 熟普洱"),
        (us, '2025 Yunnan Sourcing "Spring Morning" Raw Pu-erh Tea Cake', "春晓", "2025 春晓 生普洱 茶饼"),
        (us, '2025 Yunnan Sourcing "Heaven\'s Door" Wild Arbor Raw Pu-erh Tea Cake', "天门山", "2025 天门山 野生 生普洱"),
        (cl, "2025 Spring Hua Zhu Liang Zi Sheng / Raw Puerh Tea - 200g Cake", "滑竹梁子", "2025 滑竹梁子 春茶 生普洱 200g"),
        (cl, "2024 Spring Nannuo Banpo Lao Zhai Sheng / Raw Puerh Tea - 200g Cake", "半坡老寨", "2024 南糯 半坡老寨 生普洱 200g"),
        (cl, "2023 Yiwu Xiao Gaogan Sheng / Raw Puerh Tea - 200g Cake", "小高杆", "2023 易武 小高杆 生普洱 200g"),
        (cl, '2019 Xiaguan "Holy Flame" Baoyan Sheng / Raw Puerh Tea', "宝焰", "2019 下关 宝焰 生普洱"),
        (cl, '2000 "Old Warrior" Shou / Ripe Puerh 250g Golden Melon', "老勇士", "2000 老勇士 熟普洱 金瓜 250g"),
        (cl, '2023 Jingmai "Lucky Cloud" Shou / Ripe Puerh Tea 357g Cake', "祥云", "2023 景迈 祥云 熟普洱 357g"),
        (com, 'Man Gang Village "Huang Pian" Ripe Pu-erh Tea', "黄片", 'Man Gang Village "黄片" Ripe Pu-erh Tea'),
        (w2t, "2001 Big Red Mark Jia Ji", "甲级", "2001 Big Red Mark 甲级"),
        (w2t, "2015 Tuhao as #$&%", "土豪", "2015 土豪"),
        (w2t, "Black Dawn Oolong", "黑旦", "黑旦 Oolong"),
    ]
    cjk_sides: dict[str, PairSide] = {}
    for v, title, token, zh in cjk:
        base = shop(v, title)
        it = CAT.item(v, title)
        if token not in it.body and token not in it.title:
            raise SystemExit(f"CJK token {token!r} not in body of {title!r}")
        d = derive(base, zh, "cjk_from_own_body_html", token)
        cjk_sides[title] = d
        tags = ["hanzi"] + (["mixed_script"] if re.search(r"[A-Za-z]{3,}", zh.split(" ", 1)[-1]) else [])
        if title in ('2025 Yunnan Sourcing "Spring Morning" Raw Pu-erh Tea Cake',
                     '2025 Yunnan Sourcing "Heaven\'s Door" Wild Arbor Raw Pu-erh Tea Cake',
                     '2025 Yunnan Sourcing "Lotus River Dream" Ripe Pu-erh Tea Cake'):
            tags.append("translation_not_transliteration")
        add(base, d, "match", "high", "cross_script",
            f"Chinese rendering of the same product; '{token}' appears in this listing's own "
            "body_html. Trigram has ~no overlap across scripts.", tags)
    # cross-script controls (non-matches) so a scorer can't learn "CJK ⇒ match".
    for t_zh_of, t_other, v in [
        ('2025 Yunnan Sourcing "Bing Dao Lao Zhai" Raw Pu-erh Tea Cake', '2025 Yunnan Sourcing "Mo Lie Shan" Raw Pu-erh Tea Cake', us),
        ('2025 Yunnan Sourcing "Mo Lie Shan" Raw Pu-erh Tea Cake', '2025 Yunnan Sourcing "San Ke Shu" Old Arbor Raw Pu-erh Tea Cake', us),
        ('2025 Yunnan Sourcing "Ba Wang" Ripe Pu-erh Tea Cake', '2025 Yunnan Sourcing "Zhang Jia San Dui" Ripe Pu-erh Tea Cake', us),
        ("2025 Spring Hua Zhu Liang Zi Sheng / Raw Puerh Tea - 200g Cake", "2025 Spring Bulang Sheng / Raw Puerh Tea - 200g Cake", cl),
    ]:
        other = shop(v, t_other)
        zh = cjk_sides[t_zh_of]
        zh = PairSide(**{**zh.model_dump(), "weight_grams": other.weight_grams})
        add(zh, other, "non_match", "high", "cross_script",
            "Control: Chinese rendering of one product vs a different same-vendor/year product.",
            ["hanzi", "control"])
    roman = [  # (vendor, title, old, new, attested form)
        (us, '2025 Yunnan Sourcing "Bing Dao Lao Zhai" Raw Pu-erh Tea Cake', "Bing Dao", "Bingdao", "Bingdao"),
        (us, '2023 Yunnan Sourcing "Bang Dong Zi Cha" Purple Raw Pu-erh Tea Cake', "Bang Dong", "Bangdong", "Bangdong"),
        (cl, "2023 Bangdong Old Tree Sheng / Raw Puerh Tea - 200g Cake", "Bangdong", "Bang Dong", "Bang Dong"),
        (us, '2025 Yunnan Sourcing "Lao Man\'e Old Tree" Ripe Pu-erh Tea Cake', "Lao Man'e", "Lao Man'E", "Lao Man'E"),
        (cl, "2023 Yiwu Xiao Gaogan Sheng / Raw Puerh Tea - 200g Cake", "Yiwu", "Yi Wu", "Yi Wu"),
        (cl, "2021 Pasha Shan Shou / Ripe Puerh Tea", "Puerh", "Pu-erh", "Pu-erh"),
        (cl, "2025 Spring Hua Zhu Liang Zi Sheng / Raw Puerh Tea - 200g Cake", "Puerh", "Pu'erh", "Pu'erh"),
        (us, '2024 Yunnan Sourcing "Huang Shan Gu Shu" Old Arbor Raw Pu-erh Tea Cake', "Gu Shu", "Gushu", "Gushu"),
        (us, '2010 Jinggu Lao Cha Tou Ripe Pu-erh Tea', "Lao Cha Tou", "Laochatou", "Laochatou"),
        (w2t, "Classic Tie Guan Yin", "Tie Guan Yin", "Tieguanyin", "Tieguanyin"),
        (w2t, "Dahongpao", "Dahongpao", "Da Hong Pao", "Da Hong Pao"),
        (w2t, "Daily Jinjunmei", "Jinjunmei", "Jin Jun Mei", "Jin Jun Mei"),
        (w2t, "Shuixian", "Shuixian", "Shui Xian", "Shui Xian"),
        (w2t, "2014 Shoumei", "Shoumei", "Shou Mei", "Shou Mei"),
        (w2t, "2022 Double Smoke Fuzhuan", "Fuzhuan", "Fu Zhuan", "Fu Zhuan"),
        (us, '2017 Kokang Region "Cloud Mountain, Old Tree" Raw Pu-erh Tea Cake', "Pu-erh", "Puer", "Puer"),
    ]
    for v, title, old, new, attested in roman:
        base = shop(v, title)
        assert old in title, (old, title)
        add(base, derive(base, title.replace(old, new), "romanization_variant", attested),
            "match", "high", "cross_script",
            f"Romanization/spacing variant '{old}' → '{new}' (form attested elsewhere in the catalog).",
            ["romanization"])
    st_lbz = steep("2017 Spring Lao Ban Zhang Single Tree Gushu")
    add(st_lbz, synth("2017 Spring Laobanzhang Single Tree Gushu", st_lbz), "match", "high", "cross_script",
        "Synthetic spacing variant (Laobanzhang is not attested in repo data).", ["romanization"])
    xh = shop(us, '2005 Xinghai "Lao Ban Zhang" Ripe Pu-erh Tea Cake')
    add(xh, synth('2005 Xinghai "Laobanzhang" Ripe Puerh Tea Cake', xh), "match", "high", "cross_script",
        "Synthetic spacing variant of the Lao Ban Zhang village name.", ["romanization"])
    add(xh, synth("2005 兴海 老班章 熟饼", xh), "match", "high", "cross_script",
        "Synthetic Chinese rendering (老班章 / 兴海 not attested in repo data).", ["hanzi"])
    dhp = shop(us, 'Da Hong Pao "Big Red Robe" Wu Yi Shan Rock Oolong Tea')
    add(dhp, synth("大红袍 武夷山岩茶", dhp), "match", "high", "cross_script",
        "Synthetic Chinese rendering of Da Hong Pao (大红袍 only attested as a clay name, so synthetic).",
        ["hanzi"])
    add(dhp, synth('Dahongpao "Big Red Robe" Wuyishan Rock Oolong Tea', dhp), "match", "high", "cross_script",
        "Synthetic concatenated-pinyin variant ('Wuyishan' not attested).", ["romanization"])
    st_beas = steep("2015 Crimson Lotus Baiying 'Whispering Sunshine'")
    add(st_beas, derive(st_beas, "2015 Crimson Lotus Bai Ying Shan 'Whispering Sunshine'",
                        "romanization_variant", "Bai Ying Shan"),
        "match", "high", "cross_script",
        "Baiying vs 'Bai Ying Shan' (shan = mountain); CL Shopify uses the spaced form.", ["romanization"])
    dy = shop(us, '2023 Menghai "7572" Ripe Pu-erh Tea Cake')
    add(dy, synth("2023 大益 7572 熟饼", dy), "match", "high", "cross_script",
        "Synthetic Chinese rendering with the recipe code preserved (大益 not attested).", ["hanzi", "recipe"])
    add(synth("2023 大益 7262 熟饼", dy), synth("2023 大益 7572 熟饼", dy), "non_match", "high",
        "hard_negative_recipe_code", "Synthetic CJK pair differing only in recipe code.", ["hanzi", "recipe"])

    # ------------------------------------------------------------------
    # cross_vendor_lookalike — similar names, different producers (blocking should save these)
    # ------------------------------------------------------------------
    look = [
        (cl, "2023 Bangdong Old Tree Sheng / Raw Puerh Tea - 200g Cake", us, '2023 Yunnan Sourcing "Bang Dong Zi Cha" Purple Raw Pu-erh Tea Cake'),
        (cl, "2024 Spring Lao Man'E Gushu  Sheng / Raw Puerh Tea - 200g Cake", us, '2024 Yunnan Sourcing "Lao Man\'e Old Tree" Ripe Pu-erh Tea Cake'),
        (cl, "2022 Spring Naka Big Tree 200g Cake - Sheng / Raw Puerh Tea", us, '2022 Yunnan Sourcing "Naka Beauty" Old Arbor Raw Pu-erh Tea Cake'),
        (w2t, "2008 Menghai Raw Puer", us, "2005 Liming 7540 Raw Pu-erh Tea Cake"),
        (w2t, "Dahongpao", us, 'Da Hong Pao "Big Red Robe" Wu Yi Shan Rock Oolong Tea'),
        (w2t, "Red Red Robe", us, 'Da Hong Pao "Big Red Robe" Wu Yi Shan Rock Oolong Tea'),
        (w2t, "Daily Duck Shit Dancong", us, "Classical Duck Shit Dan Cong Oolong Tea"),
        (w2t, "2019 Lu'An Gongjian", us, '2019 Jingyang Fu "Fu Aroma on the Silk Road" Brick Tea'),
        (cl, "2021 Pasha Shan Shou / Ripe Puerh Tea", us, '2025 Yunnan Sourcing "Xi Niu Tang Pasha" Raw Pu-erh Tea Cake'),
        (cl, '2018 Jingmai "Lao Cha Tou" Shou Puerh Tea', us, '2018 Yunnan Sourcing "Jingmai Mountain" Ripe Pu-erh Tea Cake'),
        (w2t, "2015 Anhua Tianjian", us, '2016 Cha Yu Lin "Monkey Jian" Tian Jian Hei Cha Brick Tea'),
        (w2t, "Everyday Tieguanyin", us, "Wild Tie Guan Yin Oolong Tea"),
    ]
    for v1, t1, v2, t2 in look:
        add(shop(v1, t1), shop(v2, t2), "non_match", "high", "cross_vendor_lookalike",
            "Different vendors' different products that share origin/style tokens.")

    # ------------------------------------------------------------------
    # easy_negative — same (vendor, producer, year, weight) block, unrelated names
    # (blocking passes; only the score decides). Deterministic sha256 sampling.
    # ------------------------------------------------------------------
    for v, cap in ((w2t, 8), (us, 8), (cl, 4), (com, 4)):
        blocks: dict[tuple[Any, ...], list[str]] = defaultdict(list)
        for t, it in CAT.shop[v].items():
            if not is_tea(it) or t.endswith(SEATTLE):
                continue
            ws = [w for w in it.weights if w is not None]
            if ws:
                blocks[(it.producer, it.year, max(ws))].append(t)
        cands = []
        for key, ts in blocks.items():
            for a, b in itertools.combinations(sorted(ts), 2):
                if normalize_name(strip_year(a))[:6] != normalize_name(strip_year(b))[:6]:
                    cands.append((a, b))
        n = 0
        for a, b in sorted(cands, key=lambda ab: _h("easy|" + ab[0] + "|" + ab[1])):
            if add(shop(v, a), shop(v, b), "non_match", "high", "easy_negative",
                   "Unrelated products that merely share producer / year / weight."):
                n += 1
            if n >= cap:
                break
    for v1, t1, v2, t2 in [
        (w2t, "2019 Waffles", cl, '2019 "Nevermore" Shou / Ripe Puerh Tea 357g Cake'),
        (us, '2023 Yunnan Sourcing "Cozy" Certified Organic Ripe Pu-erh Tea', w2t, "2023 Lumber Slut"),
        (cl, '2020 "The Way" Shou / Ripe Puerh Tea', w2t, "2020 Mud"),
        (com, '2006 Mengku "Ripe Tuo" Ripe Pu-erh Tea Tuo', cl, "2007 Changtai Menghai Sheng / Raw Tuo Cha (100g)"),
    ]:
        add(shop(v1, t1), shop(v2, t2), "non_match", "high", "easy_negative",
            "Unrelated products from different vendors.")


# ======================================================================
# PRODUCER PAIRS
# ======================================================================


def build_producers() -> None:
    us, com, w2t, cl = "us", "com", "w2t", "cl"
    P = producer_of
    T = producer_token
    st_ref = f"cassette:{STEEPSTER_CASSETTE}"

    def A(name: str) -> PairSide:
        return producer_attested(name, st_ref + "#tasting_notes")

    dayi_tag = P(us, '2022 Menghai "7572" Ripe Pu-erh Tea Cake')  # Da Yi (Menghai Tea Factory)
    dayi_tag2 = P(us, '2019 Menghai "7452" Ripe Pu-erh Tea Cake')  # Menghai Tea Factory (Da Yi Brand)
    assert dayi_tag.name == "Da Yi (Menghai Tea Factory)", dayi_tag.name
    assert dayi_tag2.name == "Menghai Tea Factory (Da Yi Brand)", dayi_tag2.name
    mtf = T(us, '2023 Menghai Tea Factory "Hong Yu" Ripe Pu-erh Tea Cake', "Menghai Tea Factory")
    dayi_title = T(us, "2007 Menghai DaYi V93 Premium Ripe Pu-erh Tea Tuo", "DaYi")
    slug_dayi = PairSide(name="Dayi", source_ref="config:config/vendors.yaml#steepster.vendor_slugs=dayi")
    slug_mtf = PairSide(name="Menghai Tea Factory",
                        source_ref="config:config/vendors.yaml#steepster.vendor_slugs=menghai-tea-factory")
    xg_tag = P(us, '2021 Xiaguan "Red Mark" Raw Pu-erh Tea Cake')
    ysb = P(us, '2021 Yunnan Sourcing "Companion" Raw Pu-erh Tea Cake')  # Yunnan Sourcing Brand Tea
    ysb2 = P(us, '2010 Yunnan Sourcing "Ding Jia Zhai" Autumn Harvest Yi Wu Pu-erh Tea Cake')  # Brand
    ysb3 = P(us, '2021 Yunnan Sourcing "Huang Cao Ba Village" Raw Pu-erh Tea Cake')  # Brand Pu-erh
    ysb4 = P(us, 'Certified Organic "Yunnan Mao Feng" Green Tea')  # Brand Certified Organic Teas
    ysshop = P(us, "Wild Tie Guan Yin Oolong Tea")  # Yunnan Sourcing Tea Shop
    ysusa = P(us, "2012 Rice Scent Herb & Ripe Pu-erh Tea Mini Tuo")  # Yunnan Sourcing USA
    for side, want in ((ysb, "Yunnan Sourcing Brand Tea"), (ysb2, "Yunnan Sourcing Brand"),
                       (ysb3, "Yunnan Sourcing Brand Pu-erh"),
                       (ysb4, "Yunnan Sourcing Brand Certified Organic Teas"),
                       (ysshop, "Yunnan Sourcing Tea Shop"), (ysusa, "Yunnan Sourcing USA")):
        assert side.name == want, (side.name, want)
    w2t_v = P(w2t, "2019 Waffles")
    w2t_v2 = producer_named("White2Tea")
    clt = P(cl, '2020 "The Way" Shou / Ripe Puerh Tea')
    printify = producer_named("Printify")
    limited = producer_named("Limited")
    st_clt = PairSide(name=STEEPSTER_COMPANY, source_ref=st_ref + "#company=crimson-lotus-tea", vendor="steepster")

    def tag_of(title: str, v: str = us) -> PairSide:
        return P(v, title)

    cranes1 = tag_of('2020 Three Cranes "1815 Recipe" Liu Bao Tea Brick')
    cranes2 = tag_of('Three Cranes "2503" Premium Liu Bao Tea in Basket')
    cnnp = tag_of('2003 CNNP "7262" Ripe Pu-erh Tea Cake')
    cnnp_hn = tag_of('2017 Zhong Cha "Ya Pan Ye Fang" Golden Flowers Fu Brick of Hunan', com)
    feitai = tag_of('FT 28 Years Aged "Li Li Xiang" Ripe Loose Leaf Pu-erh Tea')
    tdr = tag_of('2003 Tian Di Ren “7572” Ripe Pu-erh Tea Cake')
    ypt = tag_of('2006 YiPinTang "Menghai Lao Shu" Ripe Pu-erh Tea Cake')
    chunhai = tag_of('2010 Chunhai "Jin Ya" Ripe Pu-erh Tea Cake of Menghai')
    liming = tag_of("2005 Liming 7540 Raw Pu-erh Tea Cake")
    xinghai = tag_of('2005 Xinghai "Lao Ban Zhang" Ripe Pu-erh Tea Cake')
    zuxiang = tag_of('2022 Zu Xiang "Wu Liang Chuan Qi 888" Certified Organic Raw Pu-erh Tea')
    changtai = tag_of('2006 Changtai "Si Pu Yuan" Menghai Raw Pu-erh Tea')
    nanqiao = tag_of('2005 Nan Qiao "Liming Impression" Raw Pu-erh Tea Cake', com)
    laomane = tag_of('2011 Lao Man\'e Brand "Golden Tuo" Certified Organic Ripe Pu-erh Tea')
    hailang = tag_of('2004 Hai Lang Hao "Qiao Mu" Ripe Pu-erh Tea Brick')
    jjq = tag_of('Da Hong Pao Clay "Gu Yuan" Yixing Teapot * 80ml')
    mengku = tag_of('2006 Mengku "Ripe Tuo" Ripe Pu-erh Tea Tuo', com)
    chayulin = tag_of('2016 Cha Yu Lin "Monkey Jian" Tian Jian Hei Cha Brick Tea')
    baishaxi = tag_of('2012 Bai Sha Xi "Anhua Te Zhi" Hua Zhuan Tea from Hunan')
    gaojiashan = tag_of('2013 Gao Jia Shan "Waffle Cut" Qian Liang Hei Cha Tea')
    changdahao = tag_of('2006 Chang Da Hao "Yi Wu Zheng Shan" Raw Pu-erh Tea Cake', com)

    alias = [
        (dayi_tag, dayi_tag2, "high", "Both YS tag spellings name Menghai Tea Factory / Dayi brand."),
        (dayi_tag, mtf, "high", "'Menghai Tea Factory' in a title whose producer tag is Da Yi (Menghai Tea Factory)."),
        (dayi_tag2, dayi_title, "high", "'DaYi' title token on a Da Yi-tagged product."),
        (dayi_tag, slug_dayi, "high", "Steepster has a 'dayi' company page for the same factory (config slug)."),
        (slug_mtf, slug_dayi, "high", "Steepster lists Menghai Tea Factory and Dayi as separate companies; same producer."),
        (xg_tag, T(cl, '2019 Xiaguan "Holy Flame" Baoyan Sheng / Raw Puerh Tea', "Xiaguan"), "high",
         "Bare 'Xiaguan' title token vs the YS tag 'Xiaguan Tea Factory'."),
        (ysb, ysb2, "high", "YS house brand, two tag spellings."),
        (ysb, ysb3, "high", "YS house brand, 'Pu-erh' tag variant."),
        (ysb2, ysb3, "high", "YS house brand tag variants."),
        (ysb, ysb4, "low", "YS 'Certified Organic Teas' tag — likely a sub-line of the YS house brand."),
        (ysshop, ysusa, "low", "Both denote the YS retailer itself (not a producing brand); owner to rule."),
        (w2t_v, w2t_v2, "high", "Case variant of the same white2tea vendor field."),
        (w2t_v, T(w2t, "2012 White 2 Tea / Taochaju Giant Steps", "White 2 Tea"), "high",
         "'White 2 Tea' spacing in a white2tea title."),
        (w2t_v, A("W2T"), "high", "'W2T' abbreviation used by Steepster reviewers."),
        (clt, st_clt, "high", "Shopify vendor field vs Steepster company name (identical string)."),
        (clt, T(cl, "Crimson Lotus Tea Gift Card", "Crimson Lotus"), "high", "Brand name without 'Tea'."),
        (clt, A("CLT"), "high", "'CLT' abbreviation used by Steepster reviewers."),
        (cranes1, cranes2, "high", "Both YS tags name the Three Cranes (Wuzhou Tea Factory) liu bao brand."),
        (cranes1, T(us, '2020 Three Cranes "1815 Recipe" Liu Bao Tea Brick', "Three Cranes"), "high", "Short brand name."),
        (cranes2, producer_attested("Wuzhou Tea Factory", "cassette:yunnan_sourcing_us_products.yaml#producer_tag"),
         "high", "Wuzhou Tea Factory owns the Three Cranes brand (attested in the tag string)."),
        (cnnp_hn, T(com, '2017 Zhong Cha "Ya Pan Ye Fang" Golden Flowers Fu Brick of Hunan', "Zhong Cha"), "high",
         "'Zhong Cha' (中茶) is CNNP's brand; title token on a CNNP (Hunan)-tagged product."),
        (feitai, T(us, 'FT 28 Years Aged "Li Li Xiang" Ripe Loose Leaf Pu-erh Tea', "FT"), "high",
         "'FT' title prefix on a Fei Tai-tagged product."),
        (tdr, T(us, '2003 Tian Di Ren “7572” Ripe Pu-erh Tea Cake', "Tian Di Ren"), "high", "Short factory name."),
        (ypt, T(us, '2006 YiPinTang "Menghai Lao Shu" Ripe Pu-erh Tea Cake', "YiPinTang"), "high",
         "Concatenated 'YiPinTang' title token vs the spaced tag."),
        (chunhai, T(us, '2010 Chunhai "Jin Ya" Ripe Pu-erh Tea Cake of Menghai', "Chunhai"), "high", "Short factory name."),
        (liming, T(us, "2005 Liming 7540 Raw Pu-erh Tea Cake", "Liming"), "high", "Short factory name."),
        (xinghai, T(us, '2005 Xinghai "Lao Ban Zhang" Ripe Pu-erh Tea Cake', "Xinghai"), "high", "Short factory name."),
        (zuxiang, T(us, '2022 Zu Xiang "Wu Liang Chuan Qi 888" Certified Organic Raw Pu-erh Tea', "Zu Xiang"), "high", "Short factory name."),
        (changtai, T(cl, '2003 Changtai "Ji Nian / Memorial" Sheng / Raw Tuo Cha (100g) :: Seattle Inventory', "Changtai"), "high",
         "'Changtai' in a Crimson Lotus title vs the YS tag 'Changtai Brand'."),
        (nanqiao, T(com, '2005 Nan Qiao "Liming Impression" Raw Pu-erh Tea Cake', "Nan Qiao"), "high", "Short factory name."),
        (laomane, T(us, '2011 Lao Man\'e Brand "Golden Tuo" Certified Organic Ripe Pu-erh Tea', "Lao Man'e"), "low",
         "'Lao Man'e' is also the village name; as a producer hint it most likely means the brand."),
        (mengku, T(com, '2006 Mengku "Ripe Tuo" Ripe Pu-erh Tea Tuo', "Mengku"), "low",
         "'Mengku' is also a town/region; as a producer hint it most likely means the Shuangjiang Mengku factory."),
        (hailang, synth("Hailanghao"), "high", "Synthetic concatenated spelling."),
        (chayulin, synth("Chayulin"), "high", "Synthetic concatenated spelling."),
        (gaojiashan, synth("Gaojiashan Tea Factory"), "high", "Synthetic concatenated spelling + suffix."),
        (cnnp, synth("China National Native Produce & Animal By-Products"), "high",
         "Synthetic: CNNP's expanded name (not attested)."),
        (changdahao, T(com, '2006 Chang Da Hao "Yi Wu Zheng Shan" Raw Pu-erh Tea Cake', "Chang Da Hao"), "high",
         "Identical string from title (exact-match sanity row)."),
    ]
    for a, b, conf, why in alias:
        addp(a, b, "match", conf, "producer_alias", why)

    cjk = [
        (jjq, T(com, 'Da Hong Pao Clay "Shui Ping" Yixing Teapot by Jin Jia Qi', "金家棋", "cjk_from_own_body_html"), "high",
         "金家棋 appears in the body of a Jin Jia Qi teapot listing."),
        (xg_tag, T(cl, '2019 Xiaguan "Holy Flame" Baoyan Sheng / Raw Puerh Tea', "下关", "cjk_from_own_body_html"), "high",
         "下关 appears in the body of the Xiaguan Holy Flame listing."),
        (tdr, T(us, '2003 Tian Di Ren “7572” Ripe Pu-erh Tea Cake', "天地人", "cjk_from_own_body_html"), "high",
         "天地人 appears in the body of the Tian Di Ren 7572 listing."),
        (dayi_tag, synth("大益"), "high", "Synthetic: Dayi brand in hanzi."),
        (dayi_tag2, synth("勐海茶厂"), "high", "Synthetic: Menghai Tea Factory in hanzi."),
        (xg_tag, synth("下关茶厂"), "high", "Synthetic: Xiaguan Tea Factory in hanzi."),
        (cnnp, synth("中茶"), "high", "Synthetic: Zhongcha/CNNP in hanzi."),
        (changtai, synth("昌泰"), "high", "Synthetic: Changtai in hanzi."),
        (laomane, synth("老曼峨"), "high", "Synthetic: Lao Man'e in hanzi."),
        (liming, synth("黎明茶厂"), "high", "Synthetic: Liming Tea Factory in hanzi."),
        (xinghai, synth("兴海茶厂"), "high", "Synthetic: Xinghai Tea Factory in hanzi."),
        (baishaxi, synth("白沙溪"), "high", "Synthetic: Bai Sha Xi in hanzi."),
        (cranes1, synth("三鹤"), "high", "Synthetic: Three Cranes brand in hanzi."),
        (dayi_tag, synth("Menghai 大益"), "high", "Synthetic mixed-script producer string."),
    ]
    for a, b, conf, why in cjk:
        addp(a, b, "match", conf, "cross_script", why)
    for a, b, why in [
        (synth("大益"), synth("下关茶厂"), "Synthetic control: two different factories in hanzi."),
        (synth("勐海茶厂"), synth("黎明茶厂"), "Synthetic control: shared '茶厂' (tea factory) suffix only."),
        (synth("勐海茶厂"), tdr, "Synthetic: Menghai Tea Factory (hanzi) vs Menghai Tian Di Ren — different factories."),
    ]:
        addp(a, b, "non_match", "high", "cross_script", why, ["control"])

    hard = [
        (dayi_tag2, tdr, "high", "Both Menghai factories, different producers (Dayi vs Tian Di Ren)."),
        (dayi_tag, tdr, "high", "Both Menghai factories, different producers."),
        (mtf, tdr, "high", "'Menghai Tea Factory' vs 'Menghai Tian Di Ren Tea Factory' — near-substring, different producer."),
        (ypt, dayi_tag2, "high", "Yi Pin Tang 'of Menghai' vs Menghai Tea Factory."),
        (xg_tag, xinghai, "high", "Shared 'X…a… Tea Factory' shape."),
        (tag_of('2009 Jinggu "Bamboo-Wrapped Long Brick" Ripe Pu-erh Tea'),
         tag_of("2003 Yuan Shen Ripe Pu-erh Tea Brick"), "high", "Yi Shan vs Yuan Shen Tea Factory."),
        (liming, chunhai, "high", "Two different '… Tea Factory' producers."),
        (zuxiang, tag_of('2017 "Bamboo Aroma" Tian Jian Hei Cha'), "high",
         "Zu Xiang (Yunnan pu-erh) vs Zhu Xiang Ji (Hunan tian jian) — near-homophones."),
        (nanqiao, tag_of('2009 Jinggu "Bamboo-Wrapped Long Brick" Ripe Pu-erh Tea'), "high", "Different factories."),
        (mengku, dayi_tag2, "high", "Shuangjiang Mengku vs Menghai Tea Factory — 'Meng…' prefix only."),
        (hailang, tag_of('2019 Heng Tong Hao "Golden Needle Tribute" Ripe Pu-erh Tea Cake'), "high", "Different '… Hao' producers."),
        (hailang, changdahao, "high", "Different '… Hao' producers."),
        (changtai, chunhai, "high", "Changtai vs Chunhai."),
        (changtai, laomane, "high", "Two different '… Brand' producers."),
        (cranes1, ysb3, "high", "Shared 'Brand' token only."),
        (baishaxi, gaojiashan, "high", "Two Anhua (Hunan) hei cha factories."),
        (chayulin, tag_of('2023 Chen You Shan "Fu Rong Mountain" Black Tea Fu Brick'), "high", "Different Hunan producers."),
        (cnnp, cnnp_hn, "low", "CNNP Yunnan vs CNNP (Hunan) — different provincial companies under one brand umbrella."),
        (ysb, ysshop, "low", "YS house-brand production vs YS 'Tea Shop' (third-party teas retailed by YS)."),
        (ysb3, ysshop, "low", "YS house-brand vs YS retailer tag."),
        (clt, printify, "high", "Printify is the print-on-demand merch supplier on the CL store, not a tea producer."),
        (clt, limited, "high", "'Limited' is a stray vendor-field value, not the producer."),
        (tag_of('2000 "7562 Recipe" Aged Ripe Pu-erh Tea Brick'), tag_of('2006 Menghai Gong Ting Ripe Pu-erh Tea Cake'), "low",
         ("'Other' vs 'Other Factories' — both placeholder buckets; merging them is meaningless. "
          "Owner: should placeholders be producer rows at all?")),
        (w2t_v, T(w2t, "2012 White 2 Tea / Taochaju Giant Steps", "Taochaju"), "low",
         "Taochaju is white2tea's collaboration partner on Giant Steps, not white2tea."),
    ]
    for a, b, conf, why in hard:
        addp(a, b, "non_match", conf, "hard_negative_producer", why,
             ["placeholder"] if "placeholder" in why else [])
    people = sorted({it.producer for it in CAT.shop[com].values() if it.producer}
                    | {it.producer for it in CAT.shop[us].values() if it.producer})
    person_pairs = [
        ("Wang Xu", "Wang Can"), ("Wang Xu", "Wang Ting"), ("Wang Yan Ping", "Wang Ting"),
        ("Huang Shou Zhen", "Huang Shao Tian"), ("Huang Xiang", "Huang Shou Zhen"),
        ("Xiong Liang Hui", "Xian He Ju"), ("Zhang Quan Sheng", "Zhao Li Hua"),
        ("Chen You Shan Brand", "Chen Xiao Ming"), ("She Haibiao", "Xu Nianfeng"),
        ("Hu Ying Jia", "Huang Xiang"), ("Su Mo", "Kong Ye"),
    ]
    for a, b in person_pairs:
        assert a in people and b in people, (a, b)
        addp(producer_named(a), producer_named(b),
             "non_match", "high", "hard_negative_producer",
             "Different named artisans sharing a surname / syllables.", ["artisan"])

    allp = sorted(set(people) | {clt.name, w2t_v.name})
    cands = sorted(itertools.combinations(allp, 2), key=lambda ab: _h("prod-easy|" + "|".join(ab)))
    n = 0
    for a, b in cands:
        if producer_norm_simple(a).split()[0] == producer_norm_simple(b).split()[0]:
            continue
        if {a, b} & {"Other", "Other Factories"}:
            continue
        if addp(producer_named(a), producer_named(b),
                "non_match", "high", "easy_negative", "Unrelated producers (random deterministic sample)."):
            n += 1
        if n >= 26:
            break


def producer_norm_simple(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


# ======================================================================
# Output
# ======================================================================


def materialize() -> tuple[list[LabeledPair], list[LabeledPair]]:
    out: dict[str, list[LabeledPair]] = {"product": [], "producer": []}
    counters = {"product": 0, "producer": 0}
    prefix = {"product": "prod", "producer": "prdc"}
    for s in SPECS:
        counters[s.entity] += 1
        out[s.entity].append(
            LabeledPair(
                pair_id=f"{prefix[s.entity]}-{counters[s.entity]:04d}",
                entity=s.entity,  # type: ignore[arg-type]
                left=s.left,
                right=s.right,
                label=s.label,  # type: ignore[arg-type]
                label_confidence=s.conf,  # type: ignore[arg-type]
                category=s.category,
                tags=_auto_tags(s),
                source=_source(s),  # type: ignore[arg-type]
                rationale=s.rationale,
            )
        )
    return out["product"], out["producer"]


def render(products: list[LabeledPair], producers: list[LabeledPair]) -> dict[str, str]:
    def jsonl(ps: list[LabeledPair]) -> str:
        return "".join(
            json.dumps(p.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n"
            for p in ps
        )

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["pair_id", "entity", "category", "proposed_label", "left_name", "left_attrs",
                "right_name", "right_attrs", "rationale", "owner_label", "owner_note"])
    def attrs(p: LabeledPair, s: PairSide) -> str:
        if p.entity == "producer":
            return ""
        return f"{s.producer} | {s.harvest_year} | {s.weight_grams}g"

    for p in products + producers:
        if p.label_confidence != "low":
            continue
        w.writerow([p.pair_id, p.entity, p.category, p.label, p.left.name, attrs(p, p.left),
                    p.right.name, attrs(p, p.right), p.rationale, "", ""])
    return {
        "product_pairs.jsonl": jsonl(products),
        "producer_pairs.jsonl": jsonl(producers),
        "low_confidence_review.csv": buf.getvalue(),
    }


def main(argv: list[str] | None = None) -> int:
    global CAT
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="fail if committed fixtures are stale")
    args = ap.parse_args(argv)
    CAT = load_catalog()
    build_products()
    build_producers()
    products, producers = materialize()
    files = render(products, producers)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stale = []
    for name, content in files.items():
        path = OUT_DIR / name
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(name)
        else:
            path.write_text(content, encoding="utf-8", newline="\n")
    if stale:
        print(f"stale fixtures: {stale} — re-run scripts/build_matcher_pairs.py", file=sys.stderr)
        return 1
    for label, ps in (("product", products), ("producer", producers)):
        cats: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
        for p in ps:
            c = cats[p.category]
            c[0] += 1
            c[1] += p.label == "match"
            c[2] += p.label_confidence == "low"
        print(f"{label}: {len(ps)} pairs "
              f"({sum(p.label == 'match' for p in ps)} match, "
              f"{sum(p.label_confidence == 'low' for p in ps)} low-confidence, "
              f"{sum(p.source == 'synthetic' for p in ps)} synthetic, "
              f"{sum(p.source == 'cassette_derived' for p in ps)} derived)")
        for cat in sorted(cats):
            n, m, lo = cats[cat]
            print(f"  {cat:30s} n={n:3d} match={m:3d} low={lo:2d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
