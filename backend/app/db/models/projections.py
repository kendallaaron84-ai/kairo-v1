from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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
        CheckConstraint("seed_capital >= 0", name="seed_nonnegative"),
    )
    cell_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    cell_code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    seed_capital: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(100), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(50), nullable=False)
    target_treasury_code: Mapped[str] = mapped_column(String(50), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class OwnershipTreasuryHolding(Base):
    __tablename__ = "ownership_treasury_holdings"
    __table_args__ = (
        CheckConstraint(
            "dollars_contributed >= 0", name="dollars_nonnegative"
        ),
        CheckConstraint(
            "fractional_shares >= 0", name="shares_nonnegative"
        ),
        UniqueConstraint("treasury_code", "instrument_id", name="uq_treasury_holding_instrument"),
    )
    holding_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    treasury_code: Mapped[str] = mapped_column(String(50), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("instruments.instrument_id"), nullable=False)
    dollars_contributed: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False, default=0)
    fractional_shares: Mapped[Decimal] = mapped_column(Numeric(28, 10), nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class CurrentPosition(Base):
    __tablename__ = "current_positions"
    __table_args__ = (
        CheckConstraint(
            "average_price >= 0", name="price_nonnegative"
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
