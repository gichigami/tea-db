"""Hanzi → toneless pinyin for canonical-matcher keys (ROADMAP H.2b; spec §12
"Hybrid canonical matcher" sub-q #9).

Pure module, matching-time only. Nothing here is ever written to
``product.canonical_name`` / ``producer.canonical_name`` or bronze; the raw
vendor string stays what it was (spec §11 capture-everything, design §4
quote-evidence). The only persisted output is the pinyin *alias* the
producer matcher stores next to a CJK alias (``"大益"`` → also ``"da yi"``)
so the SQL ``ANY(aliases)`` lookup can see it.

Three keys, each a strict refinement of the previous:

- :func:`pinyin_text` — every CJK run replaced by its toneless,
  space-separated pinyin; non-CJK text untouched (``"Menghai 大益"`` →
  ``"Menghai da yi"``).
- :func:`pinyin_key` — ``pinyin_text`` + NFC + lowercase + whitespace
  collapse (the producer-matcher / ``normalize_name`` normalization).
  Used as the trigram query text.
- :func:`skeleton_key` — ``pinyin_key`` with every character outside
  ``[a-z0-9]`` dropped: space-, hyphen-, apostrophe- and quote-insensitive,
  so ``"Lao Ban Zhang"`` / ``"Laobanzhang"`` / ``"老班章"`` all key to
  ``"laobanzhang"`` and ``"Pu-erh"`` / ``"Pu'erh"`` / ``"Puerh"`` agree.
  Used for exact matching only (pg_trgm splits on non-alphanumerics, so a
  space-collapsed string is a worse trigram input, not a better one —
  measured in ``tea_scrapers.eval.matcher_variants``).

Why the skeleton drops (rather than folds) accented Latin letters: the
producer matcher recomputes the same skeleton in SQL over stored aliases
(``regexp_replace(alias, '[^a-z0-9]+', '', 'g')``) and the two engines must
agree byte-for-byte. Pinyin output itself is pure ASCII (``ü`` is emitted
as ``u``), so the only casualties are accented Latin letters in vendor
strings — a recall miss on a rare spelling, never a false merge.

Known limits (documented, not fixed): pypinyin resolves polyphones from its
phrase dictionary, so a rare reading on a proper noun may come out wrong
(a recall miss); pinyin is lossy, so two different hanzi names can share a
skeleton (``西安`` / ``先`` → ``xian``). Callers that auto-match on a
skeleton hit still run the attribute guard (product) or require a unique
hit (producer).

Dependency: ``pypinyin`` (MIT, pure Python, no transitive deps; 0.84 MB
wheel / 3.9 MB installed). Imported lazily so modules that never see CJK
pay nothing; spec §8 dependency table + §12 hybrid sub-q #9 hold the §11
justification.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# CJK Unified Ideographs + Extension A + Compatibility Ideographs + the
# supplementary-plane extensions (B–F). Kana / Hangul / CJK punctuation are
# deliberately excluded: pypinyin only knows hanzi, and fullwidth
# punctuation (「」“” etc.) is handled as ordinary non-alphanumerics.
_CJK_RUN_RE = re.compile(
    "[㐀-䶿一-鿿豈-﫿\U00020000-\U0002ebef]+"
)
_WS_RE = re.compile(r"\s+")
_NON_SKELETON_RE = re.compile(r"[^a-z0-9]+")

# The SQL-side twin of ``_NON_SKELETON_RE`` (Postgres ARE). Shared so the
# producer matcher and this module cannot drift.
SKELETON_STRIP_SQL_PATTERN = "[^a-z0-9]+"

# Skeleton hits shorter than this are not trusted for auto-matching (two
# unrelated two-letter abbreviations collide too easily). Plain normalized
# equality still matches short names.
MIN_SKELETON_LEN = 3


def has_cjk(text: str | None) -> bool:
    return bool(text) and _CJK_RUN_RE.search(text) is not None


@lru_cache(maxsize=4096)
def _run_to_pinyin(run: str) -> str:
    from pypinyin import Style, lazy_pinyin

    # NORMAL = toneless; v_to_u=False emits "v" for ü, which we fold to "u"
    # (vendors write 绿 as "Lu", never "Lv" / "Lü"). "v" only ever appears in
    # pypinyin output as the ü stand-in.
    syllables = lazy_pinyin(run, style=Style.NORMAL, v_to_u=False, errors="ignore")
    return " ".join(s.replace("v", "u") for s in syllables if s)


def pinyin_text(text: str) -> str:
    """Replace each CJK run with space-padded toneless pinyin; keep the rest."""
    if not has_cjk(text):
        return text
    return _CJK_RUN_RE.sub(lambda m: f" {_run_to_pinyin(m.group(0))} ", text)


def pinyin_key(text: str) -> str:
    """NFC + pinyin + lowercase + collapsed whitespace."""
    return _WS_RE.sub(" ", pinyin_text(unicodedata.normalize("NFC", text)).lower()).strip()


def skeleton_key(text: str) -> str:
    """``pinyin_key`` with everything outside ``[a-z0-9]`` removed."""
    return _NON_SKELETON_RE.sub("", pinyin_key(text))


def usable_skeleton(text: str) -> str | None:
    """``skeleton_key`` if long enough to auto-match on, else ``None``."""
    sk = skeleton_key(text)
    return sk if len(sk) >= MIN_SKELETON_LEN else None


__all__ = [
    "MIN_SKELETON_LEN",
    "SKELETON_STRIP_SQL_PATTERN",
    "has_cjk",
    "pinyin_key",
    "pinyin_text",
    "skeleton_key",
    "usable_skeleton",
]
