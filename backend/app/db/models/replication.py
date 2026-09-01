from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CellReplicationProposal(Base):
    __tablename__ = "cell_replication_proposals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["strategy_identifier", "strategy_version"],
            ["strategy_registry.strategy_id", "strategy_registry.version_tag"],
            name="fk_replication_proposal_strategy_version",
        ),
        ForeignKeyConstraint(
            ["risk_policy_identifier"], ["risk_policies.policy_identifier"],
            name="fk_replication_proposal_risk_policy_identifier",
        ),
        CheckConstraint("proposed_seed_capital_usd > 0", name="proposal_seed_positive"),
        CheckConstraint("is_synthetic = true", name="proposals_phase4_synthetic_only"),
        CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$'", name="replication_proposal_manifest_sha256"
        ),
        CheckConstraint(
            "target_type IN ('SINGLE_ASSET', 'BASKET', 'INDEX', 'CASH_GOAL')",
            name="replication_proposals_target_type",
        ),
        UniqueConstraint("proposed_child_code", name="uq_replication_proposed_child_code"),
        UniqueConstraint("manifest_hash", name="uq_replication_proposal_manifest_hash"),
        Index("idx_replication_proposals_parent", "parent_cell_id"),
    )

    proposal_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    parent_cell_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("capital_cells.cell_id"), nullable=False
    )
    proposed_child_code: Mapped[str] = mapped_column(String(16), nullable=False)
    capital_class: Mapped[str] = mapped_column(String(32), nullable=False, default="MICRO-100-v1")
    proposed_seed_capital_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    strategy_identifier: Mapped[str] = mapped_column(String(100), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(50), nullable=False)
    risk_policy_identifier: Mapped[str] = mapped_column(String(64), nullable=False)
    target_config_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cell_treasury_configs.config_id"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_instrument_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("instruments.instrument_id"), nullable=False
    )
    target_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    target_treasury_code: Mapped[str] = mapped_column(String(50), nullable=False)
    proposed_autonomy_tier: Mapped[str] = mapped_column(
        String(32), nullable=False, default="APPRENTICE"
    )
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReplicationProposalEvent(Base):
    __tablename__ = "replication_proposal_events"
    __table_args__ = (
        CheckConstraint(
            "state_from IN ('INITIAL', 'PENDING_AUTHORIZATION', 'AUTHORIZED', "
            "'REJECTED', 'EXPIRED', 'EXECUTED', 'CANCELLED')",
            name="proposal_events_state_from",
        ),
        CheckConstraint(
            "state_to IN ('PENDING_AUTHORIZATION', 'AUTHORIZED', 'REJECTED', "
            "'EXPIRED', 'EXECUTED', 'CANCELLED')",
            name="proposal_events_state_to",
        ),
        Index("idx_proposal_events_proposal", "proposal_id", "occurred_at"),
    )

    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    proposal_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cell_replication_proposals.proposal_id"), nullable=False
    )
    state_from: Mapped[str] = mapped_column(String(32), nullable=False)
    state_to: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReplicationProposalReservation(Base):
    __tablename__ = "replication_proposal_reservations"
    __table_args__ = (
        CheckConstraint("reserved_usd > 0", name="replication_reserved_positive"),
        UniqueConstraint(
            "proposal_id", "allocation_id", name="uq_replication_proposal_allocation"
        ),
        Index("idx_replication_reservations_alloc", "allocation_id"),
        Index("idx_replication_reservations_prop", "proposal_id"),
    )

    reservation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    proposal_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cell_replication_proposals.proposal_id"), nullable=False
    )
    allocation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("siphon_allocations.allocation_id"), nullable=False
    )
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReplicationReservationEvent(Base):
    __tablename__ = "replication_reservation_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('RESERVED', 'RELEASED', 'CONSUMED')",
            name="reservation_events_type",
        ),
        Index("idx_reservation_events_res", "reservation_id", "occurred_at"),
    )

    event_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    reservation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("replication_proposal_reservations.reservation_id"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReplicationAuthorization(Base):
    __tablename__ = "replication_authorizations"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('APPROVE', 'REJECT')", name="replication_authorizations_decision"
        ),
        CheckConstraint(
            "manifest_hash ~ '^[0-9a-f]{64}$'",
            name="replication_authorizations_manifest_sha256",
        ),
    )

    authorization_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    proposal_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("cell_replication_proposals.proposal_id"),
        nullable=False,
        unique=True,
    )
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    authorized_by: Mapped[str] = mapped_column(String(64), nullable=False)
    authorization_method: Mapped[str] = mapped_column(String(32), nullable=False)
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReplicationCashConsumption(Base):
    __tablename__ = "replication_cash_consumptions"
    __table_args__ = (
        CheckConstraint("consumed_usd > 0", name="genesis_consumed_positive"),
        CheckConstraint("is_synthetic = true", name="genesis_consumed_synthetic_only"),
        UniqueConstraint(
            "proposal_id", "allocation_id", name="uq_genesis_consumption_proposal_alloc"
        ),
        Index("idx_replication_consumptions_child", "child_cell_id"),
        Index("idx_replication_consumptions_alloc", "allocation_id"),
    )

    consumption_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    proposal_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cell_replication_proposals.proposal_id"), nullable=False
    )
    child_cell_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("capital_cells.cell_id"), nullable=False
    )
    allocation_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("siphon_allocations.allocation_id"), nullable=False
    )
    consumed_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CellGenesisEvent(Base):
    __tablename__ = "cell_genesis_events"
    __table_args__ = (
        CheckConstraint("seed_capital_usd = 100.00", name="genesis_seed_exact_micro100"),
        CheckConstraint("is_synthetic = true", name="genesis_event_synthetic_only"),
    )

    genesis_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    proposal_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("cell_replication_proposals.proposal_id"),
        nullable=False,
        unique=True,
    )
    parent_cell_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("capital_cells.cell_id"), nullable=False
    )
    child_cell_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("capital_cells.cell_id"), nullable=False, unique=True
    )
    seed_capital_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
