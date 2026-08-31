from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


class TreasuryExecutionPolicyConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    configured_minimum_order_usd: Decimal = Field(default=Decimal("5.00"), gt=0)
    broker_fractional_minimum_usd: Decimal = Field(default=Decimal("1.00"), gt=0)
    max_relative_spread_bps: Decimal = Field(default=Decimal("35.0"), ge=0)
    max_quote_age_seconds: Decimal = Field(default=Decimal("1.5"), gt=0)
    share_precision: int = Field(default=6, ge=0, le=10)
    clearance: str = "PAPER_ONLY"

    @computed_field
    @property
    def effective_minimum_usd(self) -> Decimal:
        return max(self.configured_minimum_order_usd, self.broker_fractional_minimum_usd)


class TreasuryExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    execution_id: UUID
    cell_id: UUID
    target_config_id: UUID
    instrument_id: UUID
    symbol: str
    shares_executed: Decimal
    execution_price_usd: Decimal
    gross_amount_usd: Decimal
    fee_usd: Decimal
    net_amount_usd: Decimal
    market_snapshot_id: UUID
    is_synthetic: bool
    consumption_ids: list[UUID]
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "occurred_at")
