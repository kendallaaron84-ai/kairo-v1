from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

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
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CapitalCell(Base):
    __tablename__ = "capital_cells"
    __table_args__ = (
        ForeignKeyConstraint(
            ["strategy_id", "strategy_version"],
            ["strategy_registry.strategy_id", "strategy_registry.version_tag"],
            name="fk_capital_cells_strategy_version",
        ),
        CheckConstraint(
            "seed_capital >= 0", name="ck_capital_cells_seed_nonnegative"
        ),
        CheckConstraint(
            "status IN ('INITIALIZING', 'ACTIVE', 'PAUSED', 'HALTED_FOR_DAY', "
            "'REPLICATION_READY', 'DECOMMISSIONED')",
            name="valid_lifecycle_status",
        ),
        CheckConstraint(
            "autonomy_tier IN ('APPRENTICE', 'GUARDED', 'CAPITAL_BUILDER')",
            name="valid_autonomy_tier",
        ),
        CheckConstraint(
            "economic_domain IN ('LIVE', 'SYNTHETIC', 'LEGACY_MIXED')",
            name="economic_domain",
        ),
    )
    cell_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    cell_code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    seed_capital: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    autonomy_tier: Mapped[str] = mapped_column(
        String(32), nullable=False, default="APPRENTICE", server_default="APPRENTICE"
    )
    strategy_id: Mapped[str] = mapped_column(String(100), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(50), nullable=False)
    target_treasury_code: Mapped[str] = mapped_column(String(50), nullable=False)
    risk_policy_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("risk_policies.policy_id"),
        nullable=False,
        default=UUID("a0000000-0000-0000-0000-000000000001"),
    )
    economic_domain: Mapped[str] = mapped_column(
        String(32), nullable=False, default="LIVE"
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class OwnershipTreasuryHolding(Base):
    __tablename__ = "ownership_treasury_holdings"
    __table_args__ = (
        CheckConstraint(
            "dollars_contributed >= 0", name="ck_treasury_holdings_dollars_nonnegative"
        ),
        CheckConstraint(
            "fractional_shares >= 0", name="ck_treasury_holdings_shares_nonnegative"
        ),
        CheckConstraint("total_shares >= 0", name="treasury_total_shares_nonnegative"),
        CheckConstraint(
            "cumulative_cost_basis_usd >= 0", name="treasury_basis_nonnegative"
        ),
        UniqueConstraint(
            "cell_id", "instrument_id", "is_synthetic", name="uq_cell_instrument_holding"
        ),
        Index("ix_ownership_treasury_holdings_cell", "cell_id"),
    )
    holding_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    treasury_code: Mapped[str] = mapped_column(String(50), nullable=False)
    cell_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("capital_cells.cell_id"), nullable=False
    )
    instrument_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("instruments.instrument_id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    dollars_contributed: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False, default=0)
    fractional_shares: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False, default=0)
    total_shares: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    cumulative_cost_basis_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=0
    )
    average_entry_price_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, default=0
    )
    last_marked_price_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    market_value_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    unrealized_pnl_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    legacy_values_equivalent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class CurrentPosition(Base):
    __tablename__ = "current_positions"
    __table_args__ = (
        CheckConstraint(
            "average_price >= 0", name="ck_current_positions_price_nonnegative"
        ),
        UniqueConstraint(
            "cell_id", "broker_account_id", "instrument_id",
            name="uq_current_position_identity",
        ),
    )
    position_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    cell_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("capital_cells.cell_id"), nullable=False
    )
    broker_account_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("broker_accounts.broker_account_id"), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("instruments.instrument_id"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    average_price: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
