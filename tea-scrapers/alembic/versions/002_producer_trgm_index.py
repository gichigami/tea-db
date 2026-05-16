"""producer trigram index

Revision ID: 002
Revises: 001
Create Date: 2026-05-16

Adds a GIN trigram index on ``producer.canonical_name`` so the canonical
producer matcher (``normalize/canonical.py``) can drive its fuzzy step
through an index rather than a full table scan, mirroring
``idx_product_name_trgm`` on the ``product`` table.

Required day 1 — see ``CanonicalMatcher.match_or_create_producer`` step 4.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: Union[str, Sequence[str], None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # pg_trgm is already CREATEd by 001; this just hangs an index off it.
    op.execute(
        "CREATE INDEX idx_producer_name_trgm "
        "ON producer USING gin (canonical_name gin_trgm_ops);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_producer_name_trgm;")
