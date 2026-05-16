"""Bronze → silver normalization runner (spec §1, §8).

Streams ``raw_product_snapshot`` rows on/after ``--since`` through the
mapper + canonical matcher, then materializes:

- :class:`Product` rows via :class:`CanonicalMatcher.match_or_create_product`
- :class:`Vendor` rows (one per ``raw_product_snapshot.source``)
- :class:`VendorProduct` rows keyed by composite
  ``vendor_external_id = "{shopify_product_id}:{shopify_variant_id}"``
- :class:`ProductSnapshot` fact rows per ``(vendor_product, scraped_at)``

Architecture mirrors :class:`BronzeLoader`:

- One ``SilverNormalizer`` instance per CLI invocation.
- Per-batch transactions (default 500 bronze rows) — terminal DB errors
  propagate to the CLI which finalizes the ``scrape_run`` row.
- Per-batch failures are non-fatal; recorded as ``insert_errors`` and the
  next batch continues.
- After all bronze rows are processed, :func:`assign_tiers` makes one
  set-based pass over the touched product IDs.

Idempotency: re-running over identical bronze rows is a no-op because the
canonical matcher reuses existing ``product`` / ``vendor_product`` rows
and ``product_snapshot`` insert uses ``ON CONFLICT
(vendor_product_id, scraped_at) DO NOTHING``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import (
    IntegrityError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
)
from sqlalchemy.orm import Session

from tea_scrapers.config import VendorConfig, load_shopify_vendors
from tea_scrapers.normalize.canonical import (
    CanonicalMatcher,
    ProductDecision,
)
from tea_scrapers.normalize.shopify_mapper import (
    ProductFields,
    map_payload_to_products,
)
from tea_scrapers.normalize.tier import TierStats, assign_tiers
from tea_scrapers.storage.models import (
    ProductSnapshot,
    RawProductSnapshot,
    Vendor,
    VendorProduct,
)
from tea_scrapers.storage.run_tracker import RunTracker
from tea_scrapers.storage.session import get_session


@dataclass
class NormalizeStats:
    """Cumulative counts from a :class:`SilverNormalizer` run.

    ``parse_errors`` and ``insert_errors`` are intentionally split (per the
    step-6 brief): bronze-row decoding failures vs. batch integrity / FK /
    NOT-NULL failures. Bronze loader follow-up #2 is mirrored here, not
    back-applied to ``bronze.py``.
    """

    bronze_rows_read: int = 0
    skipped_non_tea: int = 0
    skipped_unmappable: int = 0
    parse_errors: int = 0
    insert_errors: int = 0
    inserted_product: int = 0
    inserted_vendor_product: int = 0
    inserted_snapshot: int = 0
    snapshot_skipped_dedup: int = 0
    by_decision: dict[str, int] = field(
        default_factory=lambda: {d.value: 0 for d in ProductDecision}
    )
    tier_promoted_a: int = 0
    tier_demoted_b: int = 0
    tier_demoted_c: int = 0
    tier_unchanged: int = 0
    by_source: dict[str, dict[str, int]] = field(default_factory=dict)

    def bucket_for(self, source: str) -> dict[str, int]:
        bucket = self.by_source.get(source)
        if bucket is None:
            bucket = {
                "bronze_rows_read": 0,
                "skipped_non_tea": 0,
                "skipped_unmappable": 0,
                "insert_errors": 0,
                "inserted_product": 0,
                "inserted_vendor_product": 0,
                "inserted_snapshot": 0,
                "snapshot_skipped_dedup": 0,
            }
            self.by_source[source] = bucket
        return bucket


class SilverNormalizer:
    """Drive bronze → silver normalization for a date window."""

    def __init__(
        self,
        *,
        since: dt.date,
        tracker: RunTracker,
        run_id: str,
        source: str | None = None,
        session_factory: Callable[[], AbstractContextManager[Session]] = get_session,
        batch_size: int = 500,
        log: structlog.stdlib.BoundLogger | None = None,
        vendor_configs: dict[str, VendorConfig] | None = None,
    ) -> None:
        self._since = since
        self._tracker = tracker
        self._run_id = run_id
        self._source_filter = source
        self._session_factory = session_factory
        self._batch_size = batch_size
        self._log = log or structlog.get_logger(__name__)
        # vendor_configs is injectable for unit tests; the CLI passes the
        # production YAML-loaded dict. Fall back to lazy-load on first use.
        self._vendor_configs = vendor_configs
        self._matcher = CanonicalMatcher(log=self._log)
        # Vendor row cache: source_key → vendor_id.
        self._vendor_cache: dict[str, int] = {}
        # Touched product IDs for the tier-assignment sweep.
        self._touched_product_ids: set[int] = set()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> NormalizeStats:
        stats = NormalizeStats()
        for batch in self._iter_bronze_batches():
            try:
                self._process_batch(batch, stats)
            except IntegrityError as exc:
                self._log.error(
                    "normalize.batch.integrity_error",
                    batch_size=len(batch),
                    error=str(exc)[:200],
                )
                self._tracker.record_error(f"IntegrityError in normalize batch: {exc}")
                stats.insert_errors += len(batch)
            except (OperationalError, InterfaceError):
                # Terminal connection failure — let it propagate to the CLI
                # so RunTracker finalizes the run as failed and the process
                # exits 2 (per spec §7).
                raise
            except SQLAlchemyError as exc:
                # A non-terminal SQL error inside the batch — log + skip.
                self._log.error(
                    "normalize.batch.sql_error",
                    batch_size=len(batch),
                    error=str(exc)[:200],
                )
                self._tracker.record_error(f"SQLAlchemyError in normalize batch: {exc}")
                stats.insert_errors += len(batch)

        # Tier sweep — single set-based pass over touched product IDs.
        if self._touched_product_ids:
            tier_stats = self._run_tier_sweep()
            stats.tier_promoted_a = tier_stats.promoted_a
            stats.tier_demoted_b = tier_stats.demoted_b
            stats.tier_demoted_c = tier_stats.demoted_c
            stats.tier_unchanged = tier_stats.unchanged

        self._log.info(
            "normalize.run.complete",
            bronze_rows_read=stats.bronze_rows_read,
            skipped_non_tea=stats.skipped_non_tea,
            skipped_unmappable=stats.skipped_unmappable,
            parse_errors=stats.parse_errors,
            insert_errors=stats.insert_errors,
            inserted_product=stats.inserted_product,
            inserted_vendor_product=stats.inserted_vendor_product,
            inserted_snapshot=stats.inserted_snapshot,
            snapshot_skipped_dedup=stats.snapshot_skipped_dedup,
            by_decision=stats.by_decision,
            tier_promoted_a=stats.tier_promoted_a,
            tier_demoted_b=stats.tier_demoted_b,
            tier_demoted_c=stats.tier_demoted_c,
            tier_unchanged=stats.tier_unchanged,
            by_source=stats.by_source,
        )
        return stats

    # ------------------------------------------------------------------
    # Bronze iteration
    # ------------------------------------------------------------------

    def _iter_bronze_batches(
        self,
    ) -> Iterator[list[RawProductSnapshot]]:
        """Stream bronze rows with ``scraped_at::date >= since``, batched.

        Note the deliberate divergence from the bronze loader's
        partition-date filter (it filters on the JSONL file path; we filter
        on the in-row ``scraped_at``). Filed as §12 OQ.
        """
        # Read in PK order so per-vendor batching stays contiguous and the
        # run is deterministic. We pull the full id list first (cheap;
        # bronze ids are BIGSERIAL ints), then stream in id chunks so we
        # don't hold a server-side cursor across the per-batch transactions
        # that follow.
        since_dt = dt.datetime.combine(
            self._since, dt.time.min, tzinfo=dt.timezone.utc
        )
        with self._session_factory() as session:
            stmt = select(RawProductSnapshot.snapshot_id).where(
                RawProductSnapshot.scraped_at >= since_dt
            )
            if self._source_filter is not None:
                stmt = stmt.where(RawProductSnapshot.source == self._source_filter)
            stmt = stmt.order_by(
                RawProductSnapshot.source.asc(),
                RawProductSnapshot.snapshot_id.asc(),
            )
            all_ids: list[int] = list(session.execute(stmt).scalars())

        for chunk_start in range(0, len(all_ids), self._batch_size):
            chunk_ids = all_ids[chunk_start : chunk_start + self._batch_size]
            with self._session_factory() as session:
                rows = (
                    session.execute(
                        select(RawProductSnapshot)
                        .where(RawProductSnapshot.snapshot_id.in_(chunk_ids))
                        .order_by(
                            RawProductSnapshot.source.asc(),
                            RawProductSnapshot.snapshot_id.asc(),
                        )
                    )
                    .scalars()
                    .all()
                )
                # Detach from session — we'll touch them inside the
                # per-batch transaction in ``_process_batch``.
                session.expunge_all()
            yield list(rows)

    # ------------------------------------------------------------------
    # Per-batch processing
    # ------------------------------------------------------------------

    def _process_batch(
        self,
        batch: list[RawProductSnapshot],
        stats: NormalizeStats,
    ) -> None:
        if not batch:
            return
        with self._session_factory() as session:
            self._matcher.forget_session(session)  # in case session ids recycle
            for bronze_row in batch:
                self._process_bronze_row(bronze_row, stats, session)
            # Ensure trgm-cache entry doesn't outlive the session.
            self._matcher.forget_session(session)

    def _process_bronze_row(
        self,
        bronze_row: RawProductSnapshot,
        stats: NormalizeStats,
        session: Session,
    ) -> None:
        stats.bronze_rows_read += 1
        bucket = stats.bucket_for(bronze_row.source)
        bucket["bronze_rows_read"] += 1

        vendor_cfg = self._lookup_vendor_config(bronze_row.source)
        try:
            field_set = map_payload_to_products(
                bronze_row.payload,
                vendor_base_url=vendor_cfg.base_url if vendor_cfg else None,
            )
        except (TypeError, ValueError, KeyError) as exc:
            # Mapper is pure-Python; any narrow failure here is a data
            # shape we hadn't seen. Count + log + move on.
            stats.parse_errors += 1
            self._tracker.record_error(
                f"normalize.parse_error source={bronze_row.source} "
                f"external_id={bronze_row.external_id}: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            )
            self._log.warning(
                "normalize.row.parse_error",
                source=bronze_row.source,
                external_id=bronze_row.external_id,
                error_type=type(exc).__name__,
            )
            return

        if not field_set:
            stats.skipped_unmappable += 1
            bucket["skipped_unmappable"] += 1
            return

        # All variants share the same product-level fields (producer / region
        # / tea_type / etc.), so we resolve those once per bronze row.
        first = field_set[0]
        if first.is_non_tea:
            stats.skipped_non_tea += 1
            bucket["skipped_non_tea"] += 1
            return

        vendor_id = self._get_or_create_vendor(session, bronze_row.source, vendor_cfg)
        producer_id = self._matcher.match_or_create_producer(
            first.producer_hint, session
        )
        region_id = self._matcher.match_or_create_region(first.region_hint, session)

        for fields in field_set:
            if fields.canonical_name == "" or fields.weight_grams is None:
                # No usable weight or name — unmappable. Counted once per
                # variant so multi-variant unmappables don't disappear.
                stats.skipped_unmappable += 1
                bucket["skipped_unmappable"] += 1
                continue
            self._materialize_variant(
                fields,
                bronze_row=bronze_row,
                vendor_id=vendor_id,
                producer_id=producer_id,
                region_id=region_id,
                session=session,
                stats=stats,
                bucket=bucket,
            )

    def _materialize_variant(
        self,
        fields: ProductFields,
        *,
        bronze_row: RawProductSnapshot,
        vendor_id: int,
        producer_id: int | None,
        region_id: int | None,
        session: Session,
        stats: NormalizeStats,
        bucket: dict[str, int],
    ) -> None:
        match_result = self._matcher.match_or_create_product(
            fields,
            producer_id=producer_id,
            region_id=region_id,
            session=session,
        )
        stats.by_decision[match_result.decision.value] = (
            stats.by_decision.get(match_result.decision.value, 0) + 1
        )
        if match_result.decision in (
            ProductDecision.CREATED,
            ProductDecision.AMBIGUOUS_CREATED,
        ):
            stats.inserted_product += 1
            bucket["inserted_product"] += 1
        self._touched_product_ids.add(match_result.product_id)

        # Composite vendor_external_id: see VendorProduct model docstring.
        vendor_external_id = (
            f"{fields.shopify_product_id}:{fields.variant.shopify_variant_id}"
        )
        vp_id, created = self._get_or_create_vendor_product(
            session,
            vendor_id=vendor_id,
            product_id=match_result.product_id,
            vendor_external_id=vendor_external_id,
            vendor_url=fields.vendor_url,
        )
        if created:
            stats.inserted_vendor_product += 1
            bucket["inserted_vendor_product"] += 1

        inserted_snap, dedup = self._insert_snapshot(
            session,
            vendor_product_id=vp_id,
            scraped_at=bronze_row.scraped_at,
            available=fields.variant.available,
            price_cents=fields.variant.price_cents,
            currency=fields.variant.currency,
            description_hash=fields.description_hash,
        )
        stats.inserted_snapshot += inserted_snap
        stats.snapshot_skipped_dedup += dedup
        bucket["inserted_snapshot"] += inserted_snap
        bucket["snapshot_skipped_dedup"] += dedup
        if inserted_snap:
            self._tracker.record_success(inserted_snap)

    # ------------------------------------------------------------------
    # Vendor + vendor_product helpers
    # ------------------------------------------------------------------

    def _lookup_vendor_config(self, source_key: str) -> VendorConfig | None:
        if self._vendor_configs is None:
            try:
                self._vendor_configs = load_shopify_vendors()
            except ValueError:
                # Config-load failure is non-fatal for normalize — we just
                # can't synthesize ``vendor_url``. Log once and continue.
                self._log.warning("normalize.vendor_config_unavailable")
                self._vendor_configs = {}
        return self._vendor_configs.get(source_key)

    def _get_or_create_vendor(
        self,
        session: Session,
        source_key: str,
        vendor_cfg: VendorConfig | None,
    ) -> int:
        cached = self._vendor_cache.get(source_key)
        if cached is not None:
            return cached

        row = session.execute(
            select(Vendor).where(Vendor.source_key == source_key)
        ).scalar_one_or_none()
        if row is not None:
            self._vendor_cache[source_key] = row.vendor_id
            return row.vendor_id

        vendor = Vendor(
            source_key=source_key,
            display_name=(vendor_cfg.display_name if vendor_cfg else source_key),
            base_url=(vendor_cfg.base_url if vendor_cfg else None),
        )
        session.add(vendor)
        session.flush()
        self._vendor_cache[source_key] = vendor.vendor_id
        return vendor.vendor_id

    def _get_or_create_vendor_product(
        self,
        session: Session,
        *,
        vendor_id: int,
        product_id: int,
        vendor_external_id: str,
        vendor_url: str | None,
    ) -> tuple[int, bool]:
        row = session.execute(
            select(VendorProduct).where(
                VendorProduct.vendor_id == vendor_id,
                VendorProduct.vendor_external_id == vendor_external_id,
            )
        ).scalar_one_or_none()
        if row is not None:
            # Refresh URL if the upstream has been republished with a new
            # handle. ``product_id`` we leave alone — the matcher might
            # disagree with a historical decision; that's V1.5 work.
            if vendor_url is not None and row.vendor_url != vendor_url:
                row.vendor_url = vendor_url
            return row.vendor_product_id, False

        vp = VendorProduct(
            vendor_id=vendor_id,
            product_id=product_id,
            vendor_external_id=vendor_external_id,
            vendor_url=vendor_url,
        )
        session.add(vp)
        session.flush()
        return vp.vendor_product_id, True

    def _insert_snapshot(
        self,
        session: Session,
        *,
        vendor_product_id: int,
        scraped_at: dt.datetime,
        available: bool,
        price_cents: int | None,
        currency: str,
        description_hash: str,
    ) -> tuple[int, int]:
        stmt = (
            pg_insert(ProductSnapshot)
            .values(
                vendor_product_id=vendor_product_id,
                scraped_at=scraped_at,
                available=available,
                price_cents=price_cents,
                currency=currency,
                description_hash=description_hash,
            )
            .on_conflict_do_nothing(constraint="uq_snapshot_vp_time")
            .returning(ProductSnapshot.snapshot_id)
        )
        result = session.execute(stmt).scalars().all()
        if result:
            return 1, 0
        return 0, 1

    # ------------------------------------------------------------------
    # Tier sweep
    # ------------------------------------------------------------------

    def _run_tier_sweep(self) -> TierStats:
        with self._session_factory() as session:
            return assign_tiers(
                session, touched_product_ids=self._touched_product_ids
            )


__all__ = ["NormalizeStats", "SilverNormalizer"]
