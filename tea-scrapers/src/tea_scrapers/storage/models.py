"""SQLAlchemy 2.0 declarative ORM mirroring alembic/versions/001_initial.py.

The migration is the source of truth for the schema; this module exists so
the loader / normalizer have typed access to silver tables and so future
``alembic revision --autogenerate`` runs have accurate metadata to diff
against. Never call ``Base.metadata.create_all()`` in production — migrations
are the only path that touches schema.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all silver/bronze tables."""


# ---------------------------------------------------------------------------
# Bronze
# ---------------------------------------------------------------------------


class RawProductSnapshot(Base):
    __tablename__ = "raw_product_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "external_id",
            "payload_hash",
            name="uq_raw_source_external_hash",
        ),
    )

    snapshot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    scraped_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)


# ---------------------------------------------------------------------------
# Silver: canonical entities
# ---------------------------------------------------------------------------


class Producer(Base):
    __tablename__ = "producer"

    producer_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    canonical_name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    aliases: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    parent_brand: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=text("NOW()"),
        nullable=True,
    )


class Region(Base):
    __tablename__ = "region"
    __table_args__ = (
        UniqueConstraint(
            "country",
            "province",
            "county",
            "mountain",
            "village",
            name="uq_region_full_path",
        ),
    )

    region_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    country: Mapped[str] = mapped_column(Text, nullable=False)
    province: Mapped[str | None] = mapped_column(Text, nullable=True)
    county: Mapped[str | None] = mapped_column(Text, nullable=True)
    mountain: Mapped[str | None] = mapped_column(Text, nullable=True)
    village: Mapped[str | None] = mapped_column(Text, nullable=True)


class Vendor(Base):
    __tablename__ = "vendor"

    vendor_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)


class Product(Base):
    __tablename__ = "product"

    product_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    producer_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("producer.producer_id"), nullable=True
    )
    region_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("region.region_id"), nullable=True
    )
    tea_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    tea_style: Mapped[str | None] = mapped_column(Text, nullable=True)
    harvest_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cultivar: Mapped[str | None] = mapped_column(Text, nullable=True)
    format: Mapped[str | None] = mapped_column(Text, nullable=True)
    weight_grams: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # A/B/C/D per design doc — populated by the normalizer on snapshot ingest.
    data_quality_tier: Mapped[str | None] = mapped_column(CHAR(1), nullable=True)
    created_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=text("NOW()"),
        nullable=True,
    )
    updated_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=text("NOW()"),
        nullable=True,
    )


class VendorProduct(Base):
    __tablename__ = "vendor_product"
    __table_args__ = (
        UniqueConstraint("vendor_id", "vendor_external_id", name="uq_vendor_external"),
    )

    vendor_product_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    vendor_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("vendor.vendor_id"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("product.product_id"), nullable=False
    )
    vendor_external_id: Mapped[str] = mapped_column(Text, nullable=False)
    vendor_url: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Silver fact
# ---------------------------------------------------------------------------


class ProductSnapshot(Base):
    __tablename__ = "product_snapshot"
    __table_args__ = (
        UniqueConstraint("vendor_product_id", "scraped_at", name="uq_snapshot_vp_time"),
    )

    snapshot_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    vendor_product_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("vendor_product.vendor_product_id"),
        nullable=False,
    )
    scraped_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    description_hash: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Extraction outputs (V1.5)
# ---------------------------------------------------------------------------


class ProductProfile(Base):
    __tablename__ = "product_profile"

    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("product.product_id"), primary_key=True
    )
    flavor_tags: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    mouthfeel: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    hou_yun: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hui_gan: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sheng_jin: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cha_qi: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    aging_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    quote_evidence: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    extracted_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=text("NOW()"),
        nullable=True,
    )
    extraction_version: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProductEmbedding(Base):
    __tablename__ = "product_embedding"

    product_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("product.product_id"), primary_key=True
    )
    # text-embedding-3-large dim. HNSW index lives in the migration.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=text("NOW()"),
        nullable=True,
    )


# ---------------------------------------------------------------------------
# Run tracking
# ---------------------------------------------------------------------------


class ScrapeRun(Base):
    __tablename__ = "scrape_run"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # running / success / partial / failed
    status: Mapped[str] = mapped_column(Text, nullable=False)
    records_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    errors_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


__all__ = [
    "Base",
    "RawProductSnapshot",
    "Producer",
    "Region",
    "Vendor",
    "Product",
    "VendorProduct",
    "ProductSnapshot",
    "ProductProfile",
    "ProductEmbedding",
    "ScrapeRun",
]
