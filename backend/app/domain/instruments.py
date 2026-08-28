from datetime import UTC, datetime
from uuid import UUID, uuid4

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class CanonicalInstrument(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_id: UUID = Field(default_factory=uuid4)
    symbol: str
    asset_class: str
    currency: str = "USD"
    exchange: str | None = None
    effective_from: datetime = Field(default_factory=lambda: datetime.now(UTC))
    retired_at: datetime | None = None


class MarketSnapshotFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshot_id: UUID = Field(default_factory=uuid4)
    instrument_id: UUID
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    payload: dict = Field(default_factory=dict)


class BrokerAccountConfiguration(BaseModel):
    broker_account_id: UUID = Field(default_factory=uuid4)
    account_key: str
    broker_name: str
    environment: str
    status: str
    effective_from: datetime = Field(default_factory=lambda: datetime.now(UTC))
    retired_at: datetime | None = None


class BrokerInstrumentCapabilityConfiguration(BaseModel):
    capability_id: UUID = Field(default_factory=uuid4)
    broker_account_id: UUID
    instrument_id: UUID
    can_trade: bool
    can_fractional: bool
    can_short: bool
    minimum_quantity: Decimal | None = Field(default=None, ge=0)


class StrategyVersionConfiguration(BaseModel):
    strategy_id: str
    version_tag: str
    display_name: str
    status: str
    configuration: dict = Field(default_factory=dict)
    registered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    retired_at: datetime | None = None
