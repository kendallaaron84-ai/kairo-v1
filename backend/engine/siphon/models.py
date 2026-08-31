from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class SiphonBucket(str, Enum):
    SAFETY_RESERVE = "SAFETY_RESERVE"
    TARGET_TREASURY = "TARGET_TREASURY"
    REPLICATION_POOL = "REPLICATION_POOL"


class TargetType(str, Enum):
    SINGLE_ASSET = "SINGLE_ASSET"
    BASKET = "BASKET"
    INDEX = "INDEX"
    CASH_GOAL = "CASH_GOAL"


class CellTreasuryConfigInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    config_id: UUID = Field(default_factory=uuid4)
    cell_id: UUID
    target_type: TargetType = TargetType.SINGLE_ASSET
    target_instrument_id: UUID
    target_symbol: str
    config_version: int = Field(default=1, gt=0)
    is_active: bool = True
    authorized_by: str = Field(min_length=1, max_length=64)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "created_at")


class SyntheticSettlementMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    settlement_evidence_type: str = "SYNTHETIC_REPLAY_SETTLEMENT"
    synthetic_settled_at: datetime
    replay_session_id: str = Field(min_length=1)
    model_version: str = "SETTLEMENT-SIM-v0.1"

    @field_validator("synthetic_settled_at")
    @classmethod
    def settled_at_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "synthetic_settled_at")

    @model_validator(mode="after")
    def evidence_type_is_canonical(self) -> Self:
        if self.settlement_evidence_type != "SYNTHETIC_REPLAY_SETTLEMENT":
            raise ValueError("unsupported synthetic settlement evidence type")
        return self


class SiphonPolicyConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    policy_id: str = "PROFIT-ALLOC-v1.0"
    policy_version: str = "1.0.0"
    safety_reserve_pct: Decimal = Decimal("0.40")
    target_treasury_pct: Decimal = Decimal("0.40")
    replication_pool_pct: Decimal = Decimal("0.20")
    minimum_siphon_threshold_usd: Decimal = Decimal("10.00")
    protected_seed_floor_usd: Decimal = Decimal("100.00")

    @model_validator(mode="after")
    def percentages_are_complete(self) -> Self:
        ratios = (
            self.safety_reserve_pct,
            self.target_treasury_pct,
            self.replication_pool_pct,
        )
        if any(ratio < 0 for ratio in ratios) or sum(ratios) != Decimal("1"):
            raise ValueError("allocation percentages must be non-negative and sum to 1")
        if self.minimum_siphon_threshold_usd <= 0 or self.protected_seed_floor_usd < 0:
            raise ValueError("policy dollar thresholds are invalid")
        return self


class ProfitAttributionItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    attribution_id: UUID
    source_fill_id: UUID
    attributed_profit_usd: Decimal = Field(gt=0)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "occurred_at")


class SiphonEventResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    siphon_id: UUID
    cell_id: UUID
    broker_account_id: UUID | None
    settlement_snapshot_id: UUID | None
    qualified_profit_usd: Decimal
    safety_reserve_usd: Decimal
    target_treasury_usd: Decimal
    replication_pool_usd: Decimal
    target_config_id: UUID
    is_synthetic: bool
    synthetic_settlement_metadata: SyntheticSettlementMetadata | None
    source_fill_ids: list[UUID]
    attributions: list[ProfitAttributionItem]
    source_manifest_hash: str = Field(min_length=64, max_length=64)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_is_aware(cls, value: datetime) -> datetime:
        return _aware(value, "occurred_at")
