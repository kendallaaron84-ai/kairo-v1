from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (
        CheckConstraint(
            "asset_class <> 'OPTION' OR (underlying_symbol IS NOT NULL "
            "AND contract_symbol IS NOT NULL AND expiration_date IS NOT NULL "
            "AND strike_price IS NOT NULL AND strike_price > 0 "
            "AND option_right IS NOT NULL AND option_right IN ('CALL', 'PUT') "
            "AND contract_multiplier IS NOT NULL AND contract_multiplier > 0 "
            "AND listing_type IS NOT NULL)",
            name="complete_option_identity",
        ),
        UniqueConstraint("contract_symbol", name="uq_instruments_contract_symbol"),
    )

    instrument_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    exchange: Mapped[str | None] = mapped_column(String(32))
    underlying_symbol: Mapped[str | None] = mapped_column(String(32))
    contract_symbol: Mapped[str | None] = mapped_column(String(64))
    expiration_date: Mapped[date | None] = mapped_column(Date)
    strike_price: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    option_right: Mapped[str | None] = mapped_column(String(8))
    contract_multiplier: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    listing_type: Mapped[str | None] = mapped_column(String(32))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StrategyRegistry(Base):
    __tablename__ = "strategy_registry"

    strategy_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    version_tag: Mapped[str] = mapped_column(String(50), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    configuration: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrustPolicy(Base):
    __tablename__ = "trust_policies"

    policy_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    version_tag: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    policy_document: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CellTreasuryConfig(Base):
    __tablename__ = "cell_treasury_configs"
    __table_args__ = (
        CheckConstraint(
            "target_type IN ('SINGLE_ASSET', 'BASKET', 'INDEX', 'CASH_GOAL')",
            name="valid_target_type",
        ),
        CheckConstraint("config_version > 0", name="positive_config_version"),
        UniqueConstraint("cell_id", "config_version", name="uq_cell_treasury_version"),
        Index(
            "uq_cell_treasury_one_active",
            "cell_id",
            unique=True,
            postgresql_where=text("is_active = true"),
        ),
    )
    config_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    cell_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("capital_cells.cell_id"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False, default="SINGLE_ASSET")
    target_instrument_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("instruments.instrument_id"), nullable=False
    )
    target_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    authorized_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
