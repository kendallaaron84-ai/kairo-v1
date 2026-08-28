from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BrokerAccount(Base):
    __tablename__ = "broker_accounts"

    broker_account_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    account_key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    broker_name: Mapped[str] = mapped_column(String(100), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BrokerInstrumentCapability(Base):
    __tablename__ = "broker_instrument_capabilities"
    __table_args__ = (
        UniqueConstraint(
            "broker_account_id", "instrument_id", "effective_from",
            name="uq_broker_capability_version",
        ),
    )

    capability_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    broker_account_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("broker_accounts.broker_account_id"), nullable=False
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("instruments.instrument_id"), nullable=False
    )
    can_trade: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    can_fractional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_short: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notional_orders_supported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    options_supported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    extended_hours_supported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    minimum_quantity: Mapped[Decimal | None] = mapped_column(Numeric(28, 10))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
