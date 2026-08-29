from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReplayMode(StrEnum):
    LEGACY = "LEGACY_REPLAY_MODE"
    RESEARCH = "RESEARCH_REPLAY_MODE"


class LegacyReplayProvenance(StrEnum):
    EXACT_OBSERVED_SAMPLES = "EXACT_OBSERVED_SAMPLES"
    RECONSTRUCTED_SAMPLES = "RECONSTRUCTED_SAMPLES"


class ResearchEventKind(StrEnum):
    TICK = "TICK"
    QUOTE = "QUOTE"
    TRADE = "TRADE"
    BAR = "BAR"


class MarketDataLineage(BaseModel):
    model_config = ConfigDict(frozen=True)

    replay_mode: ReplayMode
    source_id: str = Field(min_length=1)
    source_kind: str = Field(min_length=1)
    exact_prototype_replay: bool
    transformation: str = Field(min_length=1)
    instrument_id: UUID
    symbol: str = Field(min_length=1)

    @model_validator(mode="after")
    def fidelity_claim_matches_mode(self) -> "MarketDataLineage":
        if self.replay_mode is ReplayMode.RESEARCH and self.exact_prototype_replay:
            raise ValueError("research replay can never claim exact prototype fidelity")
        return self


class SampledPriceObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    price: Decimal = Field(gt=0)
    instrument_id: UUID
    symbol: str = Field(min_length=1)

    @model_validator(mode="after")
    def timestamp_is_aware(self) -> "SampledPriceObservation":
        if self.timestamp.tzinfo is None:
            raise ValueError("sample timestamp must be timezone-aware")
        return self


class CompletedMinuteClose(BaseModel):
    """The only completed-bar evidence emitted by legacy replay."""

    model_config = ConfigDict(frozen=True)

    minute_start: datetime
    close: Decimal = Field(gt=0)
    completed_at: datetime
    source_observation_timestamp: datetime
    instrument_id: UUID
    symbol: str = Field(min_length=1)


class LegacyReplayResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    observations: tuple[SampledPriceObservation, ...]
    completed_minutes: tuple[CompletedMinuteClose, ...]
    lineage: MarketDataLineage


class ResearchMarketEvent(BaseModel):
    """Vendor-neutral historical event; fields remain absent unless supplied."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    kind: ResearchEventKind
    instrument_id: UUID
    symbol: str = Field(min_length=1)
    price: Decimal | None = Field(default=None, gt=0)
    bid: Decimal | None = Field(default=None, gt=0)
    ask: Decimal | None = Field(default=None, gt=0)
    open: Decimal | None = Field(default=None, gt=0)
    high: Decimal | None = Field(default=None, gt=0)
    low: Decimal | None = Field(default=None, gt=0)
    close: Decimal | None = Field(default=None, gt=0)
    volume: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def source_fields_match_event(self) -> "ResearchMarketEvent":
        if self.timestamp.tzinfo is None:
            raise ValueError("research event timestamp must be timezone-aware")
        if self.kind is ResearchEventKind.QUOTE and (self.bid is None or self.ask is None):
            raise ValueError("quote events require source bid and ask")
        if self.kind is ResearchEventKind.BAR and any(
            value is None for value in (self.open, self.high, self.low, self.close)
        ):
            raise ValueError("bar events require source OHLC values")
        if self.kind in {ResearchEventKind.TICK, ResearchEventKind.TRADE} and self.price is None:
            raise ValueError("tick and trade events require a source price")
        return self


class ResearchReplayResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    events: tuple[ResearchMarketEvent, ...]
    lineage: MarketDataLineage


def _require_strict_chronology(timestamps: list[datetime]) -> None:
    if any(current <= previous for previous, current in zip(timestamps, timestamps[1:])):
        raise ValueError("market observations must be strictly chronological")


class LegacyReplayProvider:
    def __init__(
        self,
        *,
        source_id: str,
        provenance: LegacyReplayProvenance,
        instrument_id: UUID,
        symbol: str,
    ) -> None:
        self.lineage = MarketDataLineage(
            replay_mode=ReplayMode.LEGACY,
            source_id=source_id,
            source_kind=provenance.value,
            exact_prototype_replay=(
                provenance is LegacyReplayProvenance.EXACT_OBSERVED_SAMPLES
            ),
            transformation="15_SECOND_SAMPLES_TO_CLOSE_ONLY_COMPLETED_MINUTES",
            instrument_id=instrument_id,
            symbol=symbol,
        )

    def replay(
        self, observations: tuple[SampledPriceObservation, ...]
    ) -> LegacyReplayResult:
        _require_strict_chronology([item.timestamp for item in observations])
        _require_single_instrument(
            [(item.instrument_id, item.symbol) for item in observations],
            expected=(self.lineage.instrument_id, self.lineage.symbol),
        )
        completed: list[CompletedMinuteClose] = []
        current_minute: datetime | None = None
        last_observation: SampledPriceObservation | None = None

        for observation in observations:
            minute_key = observation.timestamp.replace(second=0, microsecond=0)
            if current_minute is None:
                current_minute = minute_key
                last_observation = observation
                continue
            if minute_key != current_minute:
                assert last_observation is not None
                completed.append(
                    CompletedMinuteClose(
                        minute_start=current_minute,
                        close=last_observation.price,
                        completed_at=observation.timestamp,
                        source_observation_timestamp=last_observation.timestamp,
                        instrument_id=last_observation.instrument_id,
                        symbol=last_observation.symbol,
                    )
                )
                current_minute = minute_key
            last_observation = observation

        return LegacyReplayResult(
            observations=observations,
            completed_minutes=tuple(completed),
            lineage=self.lineage,
        )


class ResearchReplayProvider:
    def __init__(
        self,
        *,
        source_id: str,
        source_kind: str,
        instrument_id: UUID,
        symbol: str,
    ) -> None:
        self.lineage = MarketDataLineage(
            replay_mode=ReplayMode.RESEARCH,
            source_id=source_id,
            source_kind=source_kind,
            exact_prototype_replay=False,
            transformation="VENDOR_NEUTRAL_CANONICAL_EVENT_INGESTION",
            instrument_id=instrument_id,
            symbol=symbol,
        )

    def ingest(
        self, events: tuple[ResearchMarketEvent, ...]
    ) -> ResearchReplayResult:
        _require_strict_chronology([item.timestamp for item in events])
        _require_single_instrument(
            [(item.instrument_id, item.symbol) for item in events],
            expected=(self.lineage.instrument_id, self.lineage.symbol),
        )
        return ResearchReplayResult(events=events, lineage=self.lineage)


def _require_single_instrument(
    identities: list[tuple[UUID, str]], *, expected: tuple[UUID, str]
) -> None:
    if any(identity != expected for identity in identities):
        raise ValueError("replay streams must contain exactly one canonical instrument")
