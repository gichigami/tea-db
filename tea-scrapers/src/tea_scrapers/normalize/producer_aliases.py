"""Curated producer-alias table: loader + idempotent seeding (ROADMAP H.2b;
spec §12 "Hybrid canonical matcher" sub-q #9 option (b)).

The curated file (``config/producer_aliases.yaml``, owned by
ontology-curator, owner-reviewed) lists, per producing entity, the spellings
that must resolve to it — abbreviations (``W2T``), brand-vs-factory names
(``Dayi`` / ``Menghai Tea Factory``) and hanzi (``大益`` / ``勐海茶厂``) that no
string-similarity score can bridge. Schema::

    version: 1
    producers:
      - canonical: "Menghai Tea Factory"
        aliases: ["Dayi", "大益", "Menghai", "勐海茶厂"]
        notes: "..."
        evidence: ["<real title or source>", ...]

**Where it lives at match time: the existing ``producer.aliases`` column.**
:func:`seed_producer_aliases` runs at the start of ``tea-scrape normalize``
and upserts every curated spelling (normalized exactly like the matcher's
``_producer_norm``, plus the toneless pinyin of any hanzi alias) into
``producer.aliases``. The matcher's step-1 ``ANY(aliases)`` lookup then
resolves them with no new code path, the table stays the single source of
truth for "what resolves to this producer", and the seeding is visible /
auditable in SQL. An in-memory lookup was rejected: it would be a second,
invisible alias store that a later normalize run without the file would
silently disagree with.

Seeding rules (merges are irreversible, so the seeder never merges):

- No existing producer row claims the canonical or any alias → insert a row
  with ``canonical_name = canonical``.
- Exactly one row claims them → union the curated keys into its aliases.
  Its ``canonical_name`` is **not** renamed (a vendor-tag canonical such as
  ``"Menghai Tea Factory (Da Yi Brand)"`` is kept; display naming is a
  separate decision).
- Several rows claim them → a pre-existing split. Seed onto the row whose
  canonical equals the curated canonical (else the lowest ``producer_id``)
  and **skip** any key another row already holds; report the conflict
  (``producer_alias_seed.conflict``). De-duplicating the split is an owner /
  data-engineer UPDATE, never automatic.

Re-running is a no-op (keys are unioned, never removed). Removing an alias
from the YAML does not remove it from the column — deliberate: learned and
seeded aliases share the column and a stale alias is recoverable, a missing
one silently re-splits a producer.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from tea_scrapers.normalize.translit import has_cjk, pinyin_key, usable_skeleton

DEFAULT_ALIAS_PATH = Path("config/producer_aliases.yaml")
SUPPORTED_VERSION = 1

_log = structlog.get_logger(__name__)


def producer_norm(name: str) -> str:
    """NFC + lowercase + collapse whitespace (== ``canonical._producer_norm``)."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", name).lower()).strip()


def alias_keys(name: str) -> list[str]:
    """Stored ``producer.aliases`` forms for one spelling.

    The matcher norm, plus the pinyin key when the spelling contains hanzi
    (``"大益"`` → ``["大益", "da yi"]``). Skeleton forms are *not* stored:
    the matcher derives them from these in SQL.
    """
    norm = producer_norm(name)
    out = [norm]
    if has_cjk(name):
        py = pinyin_key(name)
        if py and py not in out:
            out.append(py)
    return out


class ProducerAliasEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    notes: str | None = None
    evidence: list[str] = Field(default_factory=list)

    @field_validator("canonical")
    @classmethod
    def _strip_canonical(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("canonical must be non-blank")
        return v

    @field_validator("aliases")
    @classmethod
    def _strip_aliases(cls, v: list[str]) -> list[str]:
        out = [a.strip() for a in v]
        if any(not a for a in out):
            raise ValueError("aliases must be non-blank strings")
        return out

    def spellings(self) -> list[str]:
        return [self.canonical, *self.aliases]

    def stored_keys(self) -> list[str]:
        keys: list[str] = []
        for s in self.spellings():
            for k in alias_keys(s):
                if k not in keys:
                    keys.append(k)
        return keys


class ProducerAliasFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    producers: list[ProducerAliasEntry]

    @field_validator("version")
    @classmethod
    def _version(cls, v: int) -> int:
        if v != SUPPORTED_VERSION:
            raise ValueError(f"unsupported producer_aliases version {v}")
        return v


@dataclass
class AliasTable:
    """In-memory view of the curated file (used by the eval harness + seeder).

    ``resolve`` mirrors what the seeded matcher does: normalized / pinyin
    equality first, then the space- and punctuation-insensitive skeleton.
    Skeletons claimed by two different canonicals are dropped (ambiguous)
    and listed in ``ambiguous_skeletons``.
    """

    entries: list[ProducerAliasEntry]
    by_key: dict[str, str] = field(default_factory=dict)
    by_skeleton: dict[str, str] = field(default_factory=dict)
    ambiguous_skeletons: set[str] = field(default_factory=set)

    @classmethod
    def from_entries(cls, entries: Iterable[ProducerAliasEntry]) -> AliasTable:
        tbl = cls(entries=list(entries))
        seen_canon: set[str] = set()
        for e in tbl.entries:
            cn = producer_norm(e.canonical)
            if cn in seen_canon:
                raise ValueError(f"duplicate canonical producer {e.canonical!r}")
            seen_canon.add(cn)
            for k in e.stored_keys():
                prev = tbl.by_key.get(k)
                if prev is not None and prev != e.canonical:
                    raise ValueError(
                        f"alias {k!r} listed under both {prev!r} and {e.canonical!r}"
                    )
                tbl.by_key[k] = e.canonical
            for s in e.spellings():
                sk = usable_skeleton(s)
                if sk is None or sk in tbl.ambiguous_skeletons:
                    continue
                prev = tbl.by_skeleton.get(sk)
                if prev is not None and prev != e.canonical:
                    tbl.ambiguous_skeletons.add(sk)
                    del tbl.by_skeleton[sk]
                else:
                    tbl.by_skeleton[sk] = e.canonical
        return tbl

    def resolve(self, name: str, *, use_skeleton: bool = True) -> str | None:
        for k in alias_keys(name):
            hit = self.by_key.get(k)
            if hit is not None:
                return hit
        if use_skeleton:
            sk = usable_skeleton(name)
            if sk is not None:
                return self.by_skeleton.get(sk)
        return None


def load_producer_aliases(path: Path | None = None) -> AliasTable:
    """Parse + validate the curated YAML. Raises on schema / conflict errors."""
    resolved = path or DEFAULT_ALIAS_PATH
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    parsed = ProducerAliasFile.model_validate(raw)
    return AliasTable.from_entries(parsed.producers)


@dataclass
class SeedStats:
    entries: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    keys_added: int = 0
    conflicts: list[dict[str, object]] = field(default_factory=list)


def seed_producer_aliases(session: Session, table: AliasTable) -> SeedStats:
    """Idempotently upsert curated aliases into ``producer.aliases``.

    See the module docstring for the insert / union / conflict rules. Runs
    inside the caller's transaction; the caller commits.
    """
    stats = SeedStats(entries=len(table.entries))
    for entry in table.entries:
        keys = entry.stored_keys()
        rows = session.execute(
            text(
                "SELECT producer_id, canonical_name, aliases FROM producer "
                "WHERE LOWER(canonical_name) = ANY(CAST(:keys AS text[])) "
                "   OR aliases && CAST(:keys AS text[]) "
                "ORDER BY producer_id ASC"
            ),
            {"keys": keys},
        ).all()
        if not rows:
            session.execute(
                text(
                    "INSERT INTO producer (canonical_name, aliases) "
                    "VALUES (:name, CAST(:keys AS text[]))"
                ),
                {"name": entry.canonical, "keys": keys},
            )
            stats.inserted += 1
            stats.keys_added += len(keys)
            continue

        canon_norm = producer_norm(entry.canonical)
        target = next(
            (r for r in rows if producer_norm(r.canonical_name) == canon_norm), rows[0]
        )
        claimed_elsewhere: set[str] = set()
        if len(rows) > 1:
            for r in rows:
                if r.producer_id == target.producer_id:
                    continue
                claimed_elsewhere.update(r.aliases or [])
                claimed_elsewhere.add(producer_norm(r.canonical_name))
            conflict = {
                "canonical": entry.canonical,
                "target_producer_id": target.producer_id,
                "other_producer_ids": [
                    r.producer_id for r in rows if r.producer_id != target.producer_id
                ],
                "skipped_keys": sorted(k for k in keys if k in claimed_elsewhere),
            }
            stats.conflicts.append(conflict)
            _log.warning("producer_alias_seed.conflict", **conflict)

        current = list(target.aliases or [])
        new = [k for k in keys if k not in current and k not in claimed_elsewhere]
        if not new:
            stats.unchanged += 1
            continue
        session.execute(
            text(
                "UPDATE producer SET aliases = COALESCE(aliases, ARRAY[]::TEXT[]) "
                "|| CAST(:new AS text[]) WHERE producer_id = :pid"
            ),
            {"new": new, "pid": target.producer_id},
        )
        stats.updated += 1
        stats.keys_added += len(new)
    _log.info(
        "producer_alias_seed.complete",
        entries=stats.entries,
        inserted=stats.inserted,
        updated=stats.updated,
        unchanged=stats.unchanged,
        keys_added=stats.keys_added,
        conflicts=len(stats.conflicts),
    )
    return stats


__all__ = [
    "DEFAULT_ALIAS_PATH",
    "AliasTable",
    "ProducerAliasEntry",
    "ProducerAliasFile",
    "SeedStats",
    "alias_keys",
    "load_producer_aliases",
    "producer_norm",
    "seed_producer_aliases",
]
