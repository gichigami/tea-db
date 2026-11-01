"""Which string gets embedded for a product / producer name (ROADMAP H.3).

Spec §12 hybrid sub-question #8: ``product.canonical_name`` is
order-dependent under suffix matching (a ``":: Seattle Inventory"`` listing
normalized first leaks its suffix into ``canonical_name``). Embedding
``canonical_name`` would therefore make ``text_hash`` — and the vector —
depend on normalize order. We embed the **matching key** instead:

- product: :func:`tea_scrapers.normalize.attributes.matching_key`
  (inventory suffix stripped, NFC, lower-cased, whitespace collapsed) — the
  same key the exact step compares, so "exact match" ⇔ "same text_hash".
- producer: the producer matcher's normalization (NFC + lower + collapse
  whitespace; identical to ``normalize_name`` and ``canonical._producer_norm``).

Lower-casing costs BGE-M3 little on names (its XLM-R vocabulary is cased,
but the model is robust to case) and buys hash stability across vendors that
title-case differently. Whatever H.4 decides for sub-question #8, the
embedding key does not move.
"""

from __future__ import annotations

from tea_scrapers.normalize.attributes import matching_key
from tea_scrapers.normalize.shopify_mapper import normalize_name


def product_embedding_text(name: str) -> str:
    return matching_key(name)


def producer_embedding_text(name: str) -> str:
    return normalize_name(name)


__all__ = ["producer_embedding_text", "product_embedding_text"]
