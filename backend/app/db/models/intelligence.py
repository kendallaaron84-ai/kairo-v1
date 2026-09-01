from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
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
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
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
        Index("idx_evidence_effective_at", "effective_at"),
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
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
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


class IntelligenceInvestigationCase(Base):
    __tablename__ = "intelligence_investigation_cases"
    __table_args__ = (
        CheckConstraint("btrim(query_prompt) <> ''", name="ck_case_query_not_blank"),
        CheckConstraint(
            "btrim(hypothesis_claim) <> ''", name="ck_case_hypothesis_not_blank"
        ),
        Index("idx_cases_symbol", "target_symbol"),
    )

    case_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    case_number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    query_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    hypothesis_claim: Mapped[str] = mapped_column(Text, nullable=False)
    target_symbol: Mapped[str | None] = mapped_column(String(32))
    target_theme: Mapped[str | None] = mapped_column(String(64))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IntelligenceCaseFinding(Base):
    __tablename__ = "intelligence_case_findings"
    __table_args__ = (
        CheckConstraint(
            "finding_type IN ('SUPPORTING', 'CONTRADICTORY', "
            "'GAP_IDENTIFIED', 'SUPERSEDED_FACT')",
            name="ck_case_finding_type",
        ),
        CheckConstraint(
            "(finding_type = 'GAP_IDENTIFIED' "
            "AND search_scope_json IS NOT NULL "
            "AND jsonb_typeof(search_scope_json) = 'object' "
            "AND search_scope_json <> '{}'::jsonb) "
            "OR (finding_type <> 'GAP_IDENTIFIED')",
            name="ck_gap_finding_requires_search_scope",
        ),
        CheckConstraint(
            "sequence_num > 0", name="ck_case_finding_sequence_positive"
        ),
        UniqueConstraint(
            "case_id", "sequence_num", name="uq_case_finding_sequence"
        ),
        Index("idx_findings_case_id", "case_id"),
    )

    finding_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    case_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("intelligence_investigation_cases.case_id"),
        nullable=False,
    )
    finding_type: Mapped[str] = mapped_column(String(32), nullable=False)
    claim_assertion: Mapped[str] = mapped_column(Text, nullable=False)
    finding_narrative: Mapped[str] = mapped_column(Text, nullable=False)
    search_scope_json: Mapped[dict | None] = mapped_column(JSONB)
    sequence_num: Mapped[int] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IntelligenceFindingCitation(Base):
    __tablename__ = "intelligence_finding_citations"
    __table_args__ = (
        CheckConstraint(
            "citation_role IN ('PRIMARY_PROOF', 'CONTRADICTION', 'CONTEXT')",
            name="ck_citation_role",
        ),
        CheckConstraint(
            "temporal_status IN ('ACTIVE', 'SUPERSEDED', 'HISTORICAL_CONTEXT')",
            name="ck_citation_temporal_status",
        ),
        CheckConstraint(
            "citation_relevance >= 0.00 AND citation_relevance <= 100.00",
            name="ck_citation_relevance_range",
        ),
        UniqueConstraint(
            "finding_id", "event_id", name="uq_finding_event_citation"
        ),
        Index("idx_citations_finding_id", "finding_id"),
        Index("idx_citations_event_id", "event_id"),
    )

    citation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    finding_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("intelligence_case_findings.finding_id"),
        nullable=False,
    )
    event_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("intelligence_evidence_ledger.event_id"),
        nullable=False,
    )
    citation_role: Mapped[str] = mapped_column(String(32), nullable=False)
    temporal_status: Mapped[str] = mapped_column(String(32), nullable=False)
    citation_relevance: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IntelligenceCaseConclusion(Base):
    __tablename__ = "intelligence_case_conclusions"
    __table_args__ = (
        CheckConstraint(
            "verdict IN ('CONFIRMED', 'PARTIALLY_SUPPORTED', "
            "'UNSUPPORTED', 'INSUFFICIENT_EVIDENCE')",
            name="ck_conclusion_verdict_valid",
        ),
        CheckConstraint(
            "materiality_scope IN ('MARKET', 'SECTOR', 'THEME', 'COMPANY')",
            name="ck_conclusion_materiality_scope",
        ),
        CheckConstraint(
            "time_horizon IN ('INTRADAY', 'DAYS', 'MONTHS', 'STRUCTURAL')",
            name="ck_conclusion_time_horizon",
        ),
        CheckConstraint(
            "confidence_score >= 0.00 AND confidence_score <= 100.00",
            name="ck_conclusion_confidence_range",
        ),
        CheckConstraint(
            "case_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_conclusion_manifest_sha256",
        ),
        CheckConstraint(
            "btrim(synthesis_summary) <> ''", name="ck_conclusion_summary_not_blank"
        ),
        Index("idx_conclusions_verdict", "verdict"),
    )

    conclusion_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    case_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("intelligence_investigation_cases.case_id"),
        unique=True,
        nullable=False,
    )
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    materiality_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    time_horizon: Mapped[str] = mapped_column(String(32), nullable=False)
    synthesis_summary: Mapped[str] = mapped_column(Text, nullable=False)
    case_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MarketContextAssessment(Base):
    __tablename__ = "market_context_assessments"
    __table_args__ = (
        CheckConstraint(
            "risk_posture IN ('NORMAL', 'ELEVATED', 'HIGH_EVENT_RISK', 'CRITICAL')",
            name="ck_context_risk_posture",
        ),
        CheckConstraint(
            "authority_mode = 'OBSERVE_ONLY'",
            name="ck_context_authority_mode_observe_only",
        ),
        CheckConstraint(
            "assessment_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_context_manifest_sha256",
        ),
        CheckConstraint(
            "(macro_window_active = true AND primary_event_id IS NOT NULL) "
            "OR macro_window_active = false",
            name="ck_context_macro_event_lineage",
        ),
        Index("idx_context_cell_evaluated", "cell_id", "evaluated_at"),
    )

    assessment_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    cell_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("capital_cells.cell_id"), nullable=False
    )
    risk_posture: Mapped[str] = mapped_column(String(32), nullable=False)
    authority_mode: Mapped[str] = mapped_column(
        String(32), nullable=False, default="OBSERVE_ONLY", server_default="OBSERVE_ONLY"
    )
    macro_window_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    primary_event_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("intelligence_evidence_ledger.event_id")
    )
    active_case_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("intelligence_investigation_cases.case_id")
    )
    assessment_summary: Mapped[str] = mapped_column(Text, nullable=False)
    assessment_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OrderContextEvaluation(Base):
    __tablename__ = "order_context_evaluations"
    __table_args__ = (
        CheckConstraint(
            "counterfactual_opinion IN "
            "('WOULD_HAVE_AUTHORIZED', 'WOULD_HAVE_VETOED', 'NO_OPINION')",
            name="ck_order_context_opinion",
        ),
        CheckConstraint(
            "(counterfactual_opinion = 'WOULD_HAVE_VETOED' "
            "AND veto_reason_code IS NOT NULL) "
            "OR (counterfactual_opinion <> 'WOULD_HAVE_VETOED')",
            name="ck_veto_opinion_requires_reason",
        ),
        Index("idx_evaluations_intent_id", "intent_id"),
    )

    evaluation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    intent_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("order_intents.intent_id"),
        unique=True,
        nullable=False,
    )
    assessment_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("market_context_assessments.assessment_id"),
        nullable=False,
    )
    counterfactual_opinion: Mapped[str] = mapped_column(String(32), nullable=False)
    veto_reason_code: Mapped[str | None] = mapped_column(String(64))
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
