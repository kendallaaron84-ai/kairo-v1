from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IntelligenceRawArtifact(Base):
    __tablename__ = "intelligence_raw_artifacts"
    __table_args__ = (
        CheckConstraint("byte_size > 0", name="ck_raw_artifact_size_positive"),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_raw_artifact_sha256"
        ),
        UniqueConstraint("content_sha256", name="uq_raw_artifact_content_sha256"),
        UniqueConstraint(
            "artifact_id", "content_sha256", name="uq_raw_artifact_identity_hash"
        ),
        Index("idx_raw_artifacts_hash", "content_sha256"),
    )

    artifact_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IntelligenceEvidenceLedger(Base):
    __tablename__ = "intelligence_evidence_ledger"
    __table_args__ = (
        ForeignKeyConstraint(
            ["artifact_id", "raw_content_sha256"],
            [
                "intelligence_raw_artifacts.artifact_id",
                "intelligence_raw_artifacts.content_sha256",
            ],
            name="fk_intelligence_evidence_artifact_hash",
        ),
        CheckConstraint(
            "source_type IN ('PRIMARY', 'SECONDARY', 'SOCIAL', 'USER_SUBMITTED')",
            name="ck_evidence_source_type",
        ),
        CheckConstraint(
            "event_type IN ('EARNINGS', 'MACRO', 'REGULATORY', 'GEOPOLITICAL', "
            "'M_AND_A', 'LEGAL', 'MANAGEMENT', 'PRODUCT', 'CAPITAL_RAISE', "
            "'LEGISLATION', 'INSIDER_ACTIVITY', 'CUSTOM_CLAIM')",
            name="ck_evidence_event_type",
        ),
        CheckConstraint(
            "impact_scope IN ('MARKET', 'SECTOR', 'THEME', 'COMPANY')",
            name="ck_evidence_impact_scope",
        ),
        CheckConstraint(
            "urgency IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')",
            name="ck_evidence_urgency",
        ),
        CheckConstraint(
            "time_horizon IN ('INTRADAY', 'DAYS', 'MONTHS', 'STRUCTURAL')",
            name="ck_evidence_time_horizon",
        ),
        CheckConstraint(
            "confidence_score >= 0.00 AND confidence_score <= 100.00",
            name="ck_evidence_confidence_range",
        ),
        CheckConstraint(
            "raw_content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_evidence_raw_content_sha256",
        ),
        CheckConstraint(
            "release_status IN ('SCHEDULED', 'RELEASED', 'REVISED')",
            name="ck_evidence_release_status",
        ),
        CheckConstraint(
            "referenced_event_id IS NULL OR referenced_event_id <> event_id",
            name="ck_evidence_release_reference_semantics",
        ),
        Index("idx_evidence_published_at", "published_at"),
        Index("idx_evidence_event_type", "event_type"),
        Index("idx_evidence_release_status", "release_status"),
        Index("idx_evidence_referenced_event", "referenced_event_id"),
    )

    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    artifact_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source_uri: Mapped[str | None] = mapped_column(String(1024))
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    impact_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    urgency: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    time_horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    release_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="RELEASED", server_default="RELEASED"
    )
    referenced_event_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("intelligence_evidence_ledger.event_id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IntelligenceEntityLink(Base):
    __tablename__ = "intelligence_entity_links"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('TICKER', 'THEME', 'SECTOR', 'MACRO_FACTOR')",
            name="ck_entity_link_type",
        ),
        CheckConstraint(
            "relevance_score >= 0.00 AND relevance_score <= 100.00",
            name="ck_entity_link_relevance_range",
        ),
        CheckConstraint(
            "entity_symbol = upper(entity_symbol)", name="ck_entity_symbol_uppercase"
        ),
        UniqueConstraint(
            "event_id", "entity_type", "entity_symbol", name="uq_evidence_entity_link"
        ),
        Index("idx_entity_links_symbol", "entity_symbol"),
    )

    link_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    event_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("intelligence_evidence_ledger.event_id"),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    relevance_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
