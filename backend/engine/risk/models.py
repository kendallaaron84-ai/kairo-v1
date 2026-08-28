from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import OrderPurpose, OrderSide, OrderType


class OperationalState(StrEnum):
    DISARMED = "DISARMED"
    ARMED = "ARMED"
    LOCKED_FOR_DAY = "LOCKED_FOR_DAY"
    HALTED_HARD = "HALTED_HARD"
    FLAT_LOCKED = "FLAT_LOCKED"
    MANUAL_PAUSE = "MANUAL_PAUSE"


class RiskClassification(StrEnum):
    RISK_INCREASING = "RISK_INCREASING"
    RISK_REDUCING = "RISK_REDUCING"


class DecisionVerdict(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    REJECTED = "REJECTED"


class DisqualificationReason(StrEnum):
    NONE = "NONE"
    NOT_ARMED = "NOT_ARMED"
    SYSTEM_HALTED = "SYSTEM_HALTED"
    PROFIT_CEILING_REACHED = "PROFIT_CEILING_REACHED"
    SESSION_LOSS_LIMIT_REACHED = "SESSION_LOSS_LIMIT_REACHED"
    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    STRATEGY_CLEARANCE_MISMATCH = "STRATEGY_CLEARANCE_MISMATCH"
    BROKER_CAPABILITY_UNSUPPORTED = "BROKER_CAPABILITY_UNSUPPORTED"
    OPTION_NOTIONAL_SIZING_PROHIBITED = "OPTION_NOTIONAL_SIZING_PROHIBITED"
    CELL_EXPOSURE_EXCEEDED = "CELL_EXPOSURE_EXCEEDED"
    INSUFFICIENT_AUTHORIZED_CASH = "INSUFFICIENT_AUTHORIZED_CASH"
    NO_CLOSABLE_INVENTORY = "NO_CLOSABLE_INVENTORY"
    EXIT_EXCEEDS_POSITION_QTY = "EXIT_EXCEEDS_POSITION_QTY"
    POSITION_IDENTITY_MISMATCH = "POSITION_IDENTITY_MISMATCH"
    EXIT_WOULD_INCREASE_RISK = "EXIT_WOULD_INCREASE_RISK"
    INVALID_MARKET_TIMESTAMP = "INVALID_MARKET_TIMESTAMP"


class StrategyClearance(StrEnum):
    PAPER_ONLY = "PAPER_ONLY"
    SHADOW = "SHADOW"
    LIVE = "LIVE"


class ExecutionEnvironment(StrEnum):
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE = "LIVE"


class TransitionReason(StrEnum):
    SESSION_INITIALIZED = "SESSION_INITIALIZED"
    MANUAL_ARM = "MANUAL_ARM"
    MANUAL_PAUSE = "MANUAL_PAUSE"
    MANUAL_FLATTEN_ALL = "MANUAL_FLATTEN_ALL"
    SESSION_LOSS_LIMIT = "SESSION_LOSS_LIMIT"
    SESSION_PROFIT_CEILING = "SESSION_PROFIT_CEILING"
    CONFIRMED_FLAT = "CONFIRMED_FLAT"


class RiskSessionSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str = Field(min_length=1, max_length=64)
    trading_date: date
    market_timezone: str = "America/New_York"
    session_open: datetime
    session_close: datetime

    @model_validator(mode="after")
    def valid_window(self) -> "RiskSessionSpec":
        if self.session_open.tzinfo is None or self.session_close.tzinfo is None:
            raise ValueError("risk session timestamps must be timezone-aware")
        if self.session_close <= self.session_open:
            raise ValueError("session_close must be after session_open")
        return self


class MarketMark(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_id: UUID
    mark_price: Decimal = Field(gt=0)
    source_timestamp: datetime
    received_at: datetime

    def quote_age(self) -> timedelta | None:
        if self.source_timestamp.tzinfo is None or self.received_at.tzinfo is None:
            return None
        age = self.received_at - self.source_timestamp
        return age if age >= timedelta(0) else None


class FillAccountingEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    fill_id: UUID
    kairo_order_id: UUID
    broker_account_id: UUID
    instrument_id: UUID
    realized_pnl_delta_usd: Decimal
    commission_fees_usd: Decimal = Field(ge=0)
    slippage_usd: Decimal = Field(ge=0)
    fill_price: Decimal = Field(gt=0)
    filled_qty: Decimal = Field(gt=0)
    timestamp: datetime


class IntentEvaluationInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    intent_id: UUID
    cell_id: UUID
    strategy_id: str
    strategy_version: str
    instrument_id: UUID
    order_purpose: OrderPurpose
    side: OrderSide
    target_notional_usd: Decimal | None = Field(default=None, gt=0)
    target_quantity: Decimal | None = Field(default=None, gt=0)
    order_type: OrderType

    @model_validator(mode="after")
    def exactly_one_sizing_mode(self) -> "IntentEvaluationInput":
        if (self.target_notional_usd is None) == (self.target_quantity is None):
            raise ValueError("exactly one intent sizing mode is required")
        return self


class InstrumentRiskProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_id: UUID
    asset_class: str
    contract_multiplier: Decimal | None = Field(default=None, gt=0)


class BrokerCapabilityProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    broker_account_id: UUID
    instrument_id: UUID
    can_trade: bool
    can_fractional: bool
    can_short: bool
    notional_orders_supported: bool
    options_supported: bool
    extended_hours_supported: bool
    minimum_quantity: Decimal | None = Field(default=None, ge=0)


class PositionSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    position_id: UUID
    cell_id: UUID
    broker_account_id: UUID
    instrument_id: UUID
    quantity: Decimal
    average_price: Decimal = Field(ge=0)
    contract_multiplier: Decimal = Field(default=Decimal("1"), gt=0)


class PendingRiskOrder(BaseModel):
    model_config = ConfigDict(frozen=True)

    kairo_order_id: UUID
    intent_id: UUID
    broker_account_id: UUID
    classification: RiskClassification


class RiskEvaluationRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    intent: IntentEvaluationInput
    broker_account_id: UUID
    instrument: InstrumentRiskProfile
    capability: BrokerCapabilityProfile | None
    current_position: PositionSnapshot | None = None
    market_mark: MarketMark
    strategy_clearance: StrategyClearance
    execution_environment: ExecutionEnvironment
    authorized_trading_cash: Decimal = Field(ge=0)
    authorized_exposure_usd: Decimal = Field(ge=0)
    current_exposure_usd: Decimal = Field(ge=0)
    extended_hours_requested: bool = False


class IntentRiskMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    increases_risk: bool
    requested_cash_usd: Decimal = Field(ge=0)
    projected_exposure_usd: Decimal = Field(ge=0)
    max_contractual_loss_usd: Decimal | None = Field(default=None, ge=0)
    projected_quantity: Decimal
    requested_quantity: Decimal = Field(gt=0)


class PnLSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    fees_usd: Decimal = Field(default=Decimal("0"), ge=0)
    slippage_usd: Decimal = Field(default=Decimal("0"), ge=0)
    net_pnl: Decimal


class RiskEvaluationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: UUID
    verdict: DecisionVerdict
    reason: DisqualificationReason
    classification: RiskClassification
    metrics: IntentRiskMetrics
    operational_state: OperationalState
    session_id: str
    commands: tuple[object, ...] = ()
