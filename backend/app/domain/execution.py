from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import OrderPurpose, OrderSide, OrderType, RiskVerdict


class OrderIntentFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    intent_id: UUID = Field(default_factory=uuid4)
    cell_id: UUID
    strategy_id: str
    strategy_version: str
    instrument_id: UUID
    siphon_id: UUID | None = None
    client_order_key: str
    order_purpose: OrderPurpose
    side: OrderSide
    target_notional_usd: Decimal | None = Field(default=None, gt=0)
    target_quantity: Decimal | None = Field(default=None, gt=0)
    order_type: OrderType
    limit_price: Decimal | None = Field(default=None, gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def sizing_and_prices_are_canonical(self) -> "OrderIntentFact":
        if (self.target_notional_usd is None) == (self.target_quantity is None):
            raise ValueError("exactly one intent sizing mode is required")
        expected_prices = {
            OrderType.MARKET: (False, False),
            OrderType.LIMIT: (True, False),
            OrderType.STOP: (False, True),
        }
        expected_limit, expected_stop = expected_prices[self.order_type]
        if (self.limit_price is not None) != expected_limit:
            raise ValueError("limit price does not match order type")
        if (self.stop_price is not None) != expected_stop:
            raise ValueError("stop price does not match order type")
        return self


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
    reference_price: Decimal | None = Field(default=None, gt=0)
    contract_multiplier: Decimal | None = Field(default=None, gt=0)
    slippage_usd: Decimal | None = Field(default=None, ge=0)
    commission_fee_usd: Decimal = Field(default=Decimal("0"), ge=0)
    is_simulated: bool = False
    liquidity_fidelity_tier: str | None = None
    simulation_model: str | None = None
    simulation_policy_version: str | None = None
    source_snapshot_id: UUID | None = None
    simulation_metadata: dict = Field(default_factory=dict)
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
    risk_decision_id: UUID | None = None
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
