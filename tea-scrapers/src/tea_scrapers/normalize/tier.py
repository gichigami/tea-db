"""Data-quality tier (A/B/C) assignment (design §3).

A single set-based UPDATE per normalize run. Scoped to the
``touched_product_ids`` set so unrelated products aren't touched.

- **A** = product's latest ``product_snapshot`` (across all variants) has
  ``available = true``.
- **B** = had ``available = true`` within the last 24 months, but the
  latest snapshot is ``available = false``.
- **C** = never available, or last available > 24 months ago.

Tier **D** (reference-only, no vendor record) requires the V2 curated
catalog and is out of scope for step 6.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass
class TierStats:
    """Per-bucket tier-transition counts from the sweep."""

    promoted_a: int = 0
    demoted_b: int = 0
    demoted_c: int = 0
    unchanged: int = 0


def assign_tiers(
    session: Session, *, touched_product_ids: set[int]
) -> TierStats:
    """Recompute A/B/C for every product in ``touched_product_ids``.

    Single SQL pass: compute desired tier per product in a CTE, ``UPDATE
    ... FROM`` against it, ``RETURNING`` the rows whose tier actually
    changed (via ``IS DISTINCT FROM`` so NULL → 'A' is counted as a
    transition, not a no-op).
    """
    stats = TierStats()
    if not touched_product_ids:
        return stats

    ids = sorted(touched_product_ids)
    rows = session.execute(
        text(
            """
            WITH latest AS (
              SELECT vp.product_id,
                     ps.scraped_at,
                     ps.available,
                     ROW_NUMBER() OVER (
                       PARTITION BY vp.product_id
                       ORDER BY ps.scraped_at DESC
                     ) AS rn
              FROM product_snapshot ps
              JOIN vendor_product vp ON vp.vendor_product_id = ps.vendor_product_id
              WHERE vp.product_id = ANY(:pids)
            ),
            latest_one AS (
              SELECT product_id, scraped_at, available
              FROM latest WHERE rn = 1
            ),
            recent_avail AS (
              SELECT vp.product_id, MAX(ps.scraped_at) AS last_available_at
              FROM product_snapshot ps
              JOIN vendor_product vp ON vp.vendor_product_id = ps.vendor_product_id
              WHERE vp.product_id = ANY(:pids)
                AND ps.available = TRUE
              GROUP BY vp.product_id
            ),
            desired AS (
              SELECT p.product_id,
                     CASE
                       WHEN lo.available IS TRUE THEN 'A'
                       WHEN ra.last_available_at IS NOT NULL
                            AND ra.last_available_at >= (NOW() - INTERVAL '24 months')
                         THEN 'B'
                       ELSE 'C'
                     END AS new_tier
              FROM product p
              LEFT JOIN latest_one lo ON lo.product_id = p.product_id
              LEFT JOIN recent_avail ra ON ra.product_id = p.product_id
              WHERE p.product_id = ANY(:pids)
            )
            UPDATE product p
            SET data_quality_tier = d.new_tier,
                updated_at = NOW()
            FROM desired d
            WHERE p.product_id = d.product_id
              AND p.data_quality_tier IS DISTINCT FROM d.new_tier
            RETURNING d.new_tier
            """
        ),
        {"pids": ids},
    ).all()

    changed_by_tier: dict[str, int] = {"A": 0, "B": 0, "C": 0}
    for r in rows:
        changed_by_tier[r.new_tier] = changed_by_tier.get(r.new_tier, 0) + 1

    stats.promoted_a = changed_by_tier.get("A", 0)
    stats.demoted_b = changed_by_tier.get("B", 0)
    stats.demoted_c = changed_by_tier.get("C", 0)
    stats.unchanged = len(touched_product_ids) - sum(changed_by_tier.values())
    return stats


__all__ = ["TierStats", "assign_tiers"]
