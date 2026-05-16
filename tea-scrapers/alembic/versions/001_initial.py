"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-05-16

Full §8 DDL from specs/tea_scrapers_v1_spec.md: bronze raw_product_snapshot,
silver canonical entities (producer / region / vendor / product / vendor_product),
silver fact (product_snapshot), V1.5 extraction outputs (product_profile /
product_embedding), and the scrape_run tracking table.

Extensions: pg_trgm (trigram fuzzy match on product.canonical_name — required
day 1 per data-engineer.md) and pgvector (HNSW index on product_embedding).
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Extensions
    # ------------------------------------------------------------------
    # The pgvector extension is registered in Postgres as "vector"; the
    # docker image ``pgvector/pgvector:pg16`` ships it preinstalled. The
    # design-doc shorthand "pgvector" refers to the same extension.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")

    # ------------------------------------------------------------------
    # Bronze: raw immutable snapshots
    # ------------------------------------------------------------------
    op.create_table(
        "raw_product_snapshot",
        sa.Column("snapshot_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("scraped_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "source",
            "external_id",
            "payload_hash",
            name="uq_raw_source_external_hash",
        ),
    )
    op.create_index(
        "idx_raw_source_time",
        "raw_product_snapshot",
        ["source", sa.text("scraped_at DESC")],
    )
    op.create_index(
        "idx_raw_external",
        "raw_product_snapshot",
        ["source", "external_id"],
    )

    # ------------------------------------------------------------------
    # Silver: canonical entities
    # ------------------------------------------------------------------
    op.create_table(
        "producer",
        sa.Column("producer_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("canonical_name", sa.Text(), nullable=False, unique=True),
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("parent_brand", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=True,
        ),
    )

    op.create_table(
        "region",
        sa.Column("region_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("province", sa.Text(), nullable=True),
        sa.Column("county", sa.Text(), nullable=True),
        sa.Column("mountain", sa.Text(), nullable=True),
        sa.Column("village", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "country",
            "province",
            "county",
            "mountain",
            "village",
            name="uq_region_full_path",
        ),
    )

    op.create_table(
        "vendor",
        sa.Column("vendor_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_key", sa.Text(), nullable=False, unique=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=True),
    )

    op.create_table(
        "product",
        sa.Column("product_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column(
            "producer_id",
            sa.BigInteger(),
            sa.ForeignKey("producer.producer_id"),
            nullable=True,
        ),
        sa.Column(
            "region_id",
            sa.BigInteger(),
            sa.ForeignKey("region.region_id"),
            nullable=True,
        ),
        sa.Column("tea_type", sa.Text(), nullable=True),
        sa.Column("tea_style", sa.Text(), nullable=True),
        sa.Column("harvest_year", sa.Integer(), nullable=True),
        sa.Column("cultivar", sa.Text(), nullable=True),
        sa.Column("format", sa.Text(), nullable=True),
        sa.Column("weight_grams", sa.Integer(), nullable=True),
        sa.Column("data_quality_tier", sa.CHAR(1), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=True,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_product_producer_year",
        "product",
        ["producer_id", "harvest_year"],
    )
    # Trigram index — required day 1 (data-engineer.md). Supports the
    # fuzzy-match step of the canonical product ID matcher in §8.
    op.execute(
        "CREATE INDEX idx_product_name_trgm "
        "ON product USING gin (canonical_name gin_trgm_ops);"
    )

    op.create_table(
        "vendor_product",
        sa.Column(
            "vendor_product_id",
            sa.BigInteger(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "vendor_id",
            sa.BigInteger(),
            sa.ForeignKey("vendor.vendor_id"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            sa.BigInteger(),
            sa.ForeignKey("product.product_id"),
            nullable=False,
        ),
        sa.Column("vendor_external_id", sa.Text(), nullable=False),
        sa.Column("vendor_url", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "vendor_id",
            "vendor_external_id",
            name="uq_vendor_external",
        ),
    )

    # ------------------------------------------------------------------
    # Silver fact: daily inventory/price snapshots
    # ------------------------------------------------------------------
    op.create_table(
        "product_snapshot",
        sa.Column("snapshot_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "vendor_product_id",
            sa.BigInteger(),
            sa.ForeignKey("vendor_product.vendor_product_id"),
            nullable=False,
        ),
        sa.Column("scraped_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=True),
        sa.Column("description_hash", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "vendor_product_id",
            "scraped_at",
            name="uq_snapshot_vp_time",
        ),
    )
    op.create_index(
        "idx_snapshot_time",
        "product_snapshot",
        [sa.text("scraped_at DESC")],
    )
    # Partial index — only currently-available rows, sorted by recency.
    op.execute(
        "CREATE INDEX idx_snapshot_available "
        "ON product_snapshot (vendor_product_id, scraped_at DESC) "
        "WHERE available = TRUE;"
    )

    # ------------------------------------------------------------------
    # Extraction outputs (V1.5)
    # ------------------------------------------------------------------
    op.create_table(
        "product_profile",
        sa.Column(
            "product_id",
            sa.BigInteger(),
            sa.ForeignKey("product.product_id"),
            primary_key=True,
        ),
        sa.Column("flavor_tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("mouthfeel", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("hou_yun", sa.Integer(), nullable=True),
        sa.Column("hui_gan", sa.Integer(), nullable=True),
        sa.Column("sheng_jin", sa.Integer(), nullable=True),
        sa.Column("cha_qi", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("aging_state", sa.Text(), nullable=True),
        sa.Column("quote_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "extracted_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=True,
        ),
        sa.Column("extraction_version", sa.Text(), nullable=True),
    )

    # product_embedding — VECTOR(1536) column + HNSW index. The vector
    # type isn't modelled cleanly by alembic helpers, so issue raw SQL.
    op.execute(
        """
        CREATE TABLE product_embedding (
            product_id  BIGINT PRIMARY KEY REFERENCES product(product_id),
            embedding   VECTOR(1536),
            model       TEXT NOT NULL,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        );
        """
    )
    op.execute(
        "CREATE INDEX idx_embedding_hnsw "
        "ON product_embedding USING hnsw (embedding vector_cosine_ops);"
    )

    # ------------------------------------------------------------------
    # Run tracking
    # ------------------------------------------------------------------
    op.create_table(
        "scrape_run",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("records_count", sa.Integer(), nullable=True),
        sa.Column("errors_count", sa.Integer(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    # Drop in reverse dependency order.
    op.drop_table("scrape_run")

    op.execute("DROP INDEX IF EXISTS idx_embedding_hnsw;")
    op.execute("DROP TABLE IF EXISTS product_embedding;")

    op.drop_table("product_profile")

    op.execute("DROP INDEX IF EXISTS idx_snapshot_available;")
    op.drop_index("idx_snapshot_time", table_name="product_snapshot")
    op.drop_table("product_snapshot")

    op.drop_table("vendor_product")

    op.execute("DROP INDEX IF EXISTS idx_product_name_trgm;")
    op.drop_index("idx_product_producer_year", table_name="product")
    op.drop_table("product")

    op.drop_table("vendor")
    op.drop_table("region")
    op.drop_table("producer")

    op.drop_index("idx_raw_external", table_name="raw_product_snapshot")
    op.drop_index("idx_raw_source_time", table_name="raw_product_snapshot")
    op.drop_table("raw_product_snapshot")

    # Extensions last — other schemas in the same DB may rely on them,
    # but for a single-purpose dev DB this is the symmetric undo.
    op.execute("DROP EXTENSION IF EXISTS pg_trgm;")
    op.execute("DROP EXTENSION IF EXISTS vector;")
