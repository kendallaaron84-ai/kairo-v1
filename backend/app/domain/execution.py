from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import OrderSide, OrderType, RiskVerdict


class OrderIntentFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    intent_id: UUID = Field(default_factory=uuid4)
    cell_id: UUID
    strategy_id: str
    strategy_version: str
    instrument_id: UUID
    siphon_id: UUID | None = None
    client_order_key: str
    side: OrderSide
    quantity: Decimal = Field(gt=0)
    order_type: OrderType
    limit_price: Decimal | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RiskDecisionFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: UUID = Field(default_factory=uuid4)
    intent_id: UUID
    verdict: RiskVerdict
    reason_code: str
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    details: dict = Field(default_factory=dict)


class FillFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    fill_id: UUID = Field(default_factory=uuid4)
    kairo_order_id: UUID
    broker_account_id: UUID
    broker_fill_id: str
    instrument_id: UUID
    side: OrderSide
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    filled_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CellEventFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: UUID = Field(default_factory=uuid4)
    cell_id: UUID
    event_type: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict = Field(default_factory=dict)


class KairoOrderFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    kairo_order_id: UUID = Field(default_factory=uuid4)
    intent_id: UUID
    broker_account_id: UUID
    broker_order_id: str | None = None
    status: str
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OrderObservationFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    observation_id: UUID = Field(default_factory=uuid4)
    kairo_order_id: UUID
    broker_account_id: UUID
    broker_observation_key: str
    broker_order_id: str
    event_type: str
    status: str
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict = Field(default_factory=dict)


class CurrentPositionProjection(BaseModel):
    position_id: UUID = Field(default_factory=uuid4)
    cell_id: UUID
    broker_account_id: UUID
    instrument_id: UUID
    quantity: Decimal
    average_price: Decimal = Field(ge=0)
