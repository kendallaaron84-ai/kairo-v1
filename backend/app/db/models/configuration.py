from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Instrument(Base):
    __tablename__ = "instruments"

    instrument_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    asset_class: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    exchange: Mapped[str | None] = mapped_column(String(32))
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
