from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LiquidityFidelityTier(StrEnum):
    TIER_1_QUOTE_DEPTH = "TIER_1_QUOTE_DEPTH"
    TIER_2_TRADE_HISTORY = "TIER_2_TRADE_HISTORY"
    TIER_3_BAR_ONLY = "TIER_3_BAR_ONLY"


class ExecutionQuote(BaseModel):
    model_config = ConfigDict(frozen=True)

    snapshot_id: UUID
    instrument_id: UUID
    bid: Decimal | None = Field(default=None, gt=0)
    ask: Decimal | None = Field(default=None, gt=0)
    bid_size: Decimal | None = Field(default=None, ge=0)
    ask_size: Decimal | None = Field(default=None, ge=0)
    trade_price: Decimal | None = Field(default=None, gt=0)
    trade_size: Decimal | None = Field(default=None, gt=0)
    bar_open: Decimal | None = Field(default=None, gt=0)
    bar_high: Decimal | None = Field(default=None, gt=0)
    bar_low: Decimal | None = Field(default=None, gt=0)
    bar_close: Decimal | None = Field(default=None, gt=0)
    bar_volume: Decimal | None = Field(default=None, ge=0)
    captured_at: datetime
    fidelity_tier: LiquidityFidelityTier

    @model_validator(mode="after")
    def required_source_evidence_is_present(self) -> "ExecutionQuote":
        if self.captured_at.tzinfo is None:
            raise ValueError("execution evidence timestamp must be timezone-aware")
        if self.fidelity_tier is LiquidityFidelityTier.TIER_1_QUOTE_DEPTH:
            if any(value is None for value in (self.bid, self.ask, self.bid_size, self.ask_size)):
                raise ValueError("Tier 1 requires bid, ask, bid_size, and ask_size")
            if self.bid > self.ask:
                raise ValueError("Tier 1 bid cannot exceed ask")
        elif self.fidelity_tier is LiquidityFidelityTier.TIER_2_TRADE_HISTORY:
            if self.trade_price is None or self.trade_size is None:
                raise ValueError("Tier 2 requires a subsequent trade price and size")
        elif any(
            value is None
            for value in (self.bar_open, self.bar_high, self.bar_low, self.bar_close)
        ):
            raise ValueError("Tier 3 requires source-supported OHLC bar evidence")
        elif not (
            self.bar_high >= max(self.bar_open, self.bar_close)
            and self.bar_low <= min(self.bar_open, self.bar_close)
            and self.bar_high >= self.bar_low
        ):
            raise ValueError("Tier 3 OHLC evidence is internally inconsistent")
        return self


class PaperEngineConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    broker_account_id: UUID
    broker_code: str = "PAPER_SIM_001"
    environment: str = "PAPER"
    simulation_model: str = "PAPER-FILL-v0.1"
    gateway_ack_latency_ms: int = Field(default=15, ge=0)
    matching_latency_ms: int = Field(default=50, ge=0)
    default_slippage_bps: Decimal = Field(default=Decimal("0.0005"), ge=0)
    reject_illiquid_quotes: bool = True
    quote_depth_policy_version: str = "QUOTE-DEPTH-v0.1"
    trade_history_policy_version: str = "TRADE-PRINT-v0.1"
    bar_only_policy_version: str = "BAR-COARSE-CONSERVATIVE-v0.1"


class SimulatedFillPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    fill_id: UUID = Field(default_factory=uuid4)
    kairo_order_id: UUID
    broker_account_id: UUID
    instrument_id: UUID
    side: str
    fill_price: Decimal = Field(gt=0)
    reference_price: Decimal = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    contract_multiplier: Decimal = Field(gt=0)
    slippage_usd: Decimal = Field(ge=0)
    commission_fee_usd: Decimal = Field(default=Decimal("0.00"), ge=0)
    liquidity_fidelity_tier: LiquidityFidelityTier
    simulation_model: str
    simulation_policy_version: str
    source_snapshot_id: UUID
    simulation_metadata: dict[str, Any]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PaperExecutionReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    kairo_order_id: UUID
    broker_order_id: str
    status: str
    cumulative_filled_qty: Decimal = Field(ge=0)
    remaining_qty: Decimal = Field(ge=0)
    fill_records: list[SimulatedFillPayload]
    observation_payload: dict[str, Any]
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
