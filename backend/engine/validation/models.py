from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import OptionRight


class ArtifactRole(StrEnum):
    RAW_PROVIDER_PAYLOAD = "RAW_PROVIDER_PAYLOAD"
    NORMALIZED_RESEARCH_STREAM = "NORMALIZED_RESEARCH_STREAM"


class SourceTimestampConvention(StrEnum):
    INTERVAL_BEGIN = "INTERVAL_BEGIN"
    INTERVAL_END = "INTERVAL_END"
    TICK_ARRIVAL = "TICK_ARRIVAL"


class StreamRole(StrEnum):
    UNDERLYING_SIGNAL_BARS = "UNDERLYING_SIGNAL_BARS"
    OPTION_CHAIN_QUOTES = "OPTION_CHAIN_QUOTES"
    CONTEXT_MACRO_SERIES = "CONTEXT_MACRO_SERIES"


class CanonicalMarketBar(BaseModel):
    model_config = ConfigDict(frozen=True)
    instrument_id: UUID
    symbol: str
    interval_start_at: datetime
    completed_at: datetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def causal(self) -> "CanonicalMarketBar":
        if self.completed_at <= self.interval_start_at:
            raise ValueError("completed_at must causally follow interval start")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("OHLC range is inconsistent")
        return self


class CanonicalOptionContractQuote(BaseModel):
    model_config = ConfigDict(frozen=True)
    contract_instrument_id: UUID
    underlying_instrument_id: UUID
    underlying_symbol: str
    canonical_contract_symbol: str
    expiration_date: date
    strike_price: Decimal = Field(gt=0)
    option_right: OptionRight
    contract_multiplier: Decimal = Field(gt=0)
    listing_type: str
    bid_price: Decimal = Field(ge=0)
    ask_price: Decimal = Field(ge=0)
    bid_size: Decimal = Field(ge=0)
    ask_size: Decimal = Field(ge=0)
    volume: int | None = Field(default=None, ge=0)
    open_interest: int | None = Field(default=None, ge=0)
    liquidity_verifiable: bool


class CanonicalOptionChainSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    underlying_instrument_id: UUID
    underlying_symbol: str
    canonical_completed_at: datetime
    normalization_policy_version: str = "NORM-OPT-UTC-ENRICHED-v1"
    contracts: tuple[CanonicalOptionContractQuote, ...]


class DatasetManifest(BaseModel):
    model_config = ConfigDict(frozen=True)
    dataset_id: UUID
    dataset_name: str
    provider_name: str
    replay_mode: str = "RESEARCH_REPLAY_MODE"
    exact_prototype_replay: bool = False
    calendar_version: str
    normalization_policy_version: str
    streams: tuple[dict, ...]
    dataset_manifest_sha256: str
