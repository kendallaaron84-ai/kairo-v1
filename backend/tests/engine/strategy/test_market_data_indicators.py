from datetime import UTC, datetime, timedelta
from decimal import Decimal

from engine.strategy.indicators import PrototypeEMA9
from engine.strategy.market_data import (
    LegacyReplayProvenance,
    LegacyReplayProvider,
    ReplayMode,
    ResearchEventKind,
    ResearchMarketEvent,
    ResearchReplayProvider,
    SampledPriceObservation,
)


START = datetime(2026, 8, 28, 13, 30, 5, tzinfo=UTC)


def sample(seconds: int, price: str) -> SampledPriceObservation:
    return SampledPriceObservation(
        timestamp=START + timedelta(seconds=seconds),
        price=Decimal(price),
    )


def legacy_provider() -> LegacyReplayProvider:
    return LegacyReplayProvider(
        source_id="prototype-capture-001",
        provenance=LegacyReplayProvenance.EXACT_OBSERVED_SAMPLES,
    )


def test_legacy_provider_preserves_close_only_semantics() -> None:
    observations = (sample(0, "100"), sample(45, "101"), sample(60, "102"))
    result = legacy_provider().replay(observations)
    assert result.lineage.replay_mode is ReplayMode.LEGACY
    assert result.lineage.exact_prototype_replay is True
    assert result.observations == observations
    assert len(result.completed_minutes) == 1
    close = result.completed_minutes[0]
    assert close.close == Decimal("101")
    assert set(close.model_dump()) == {
        "minute_start",
        "close",
        "completed_at",
        "source_observation_timestamp",
    }
    reconstructed = LegacyReplayProvider(
        source_id="historical-reconstruction-001",
        provenance=LegacyReplayProvenance.RECONSTRUCTED_SAMPLES,
    ).replay(observations)
    assert reconstructed.lineage.exact_prototype_replay is False
    assert reconstructed.lineage.source_kind == "RECONSTRUCTED_SAMPLES"


def test_legacy_minute_close_occurs_on_first_observation_of_next_minute() -> None:
    first_next_minute = sample(60, "102")
    result = legacy_provider().replay(
        (sample(0, "100"), sample(45, "101"), first_next_minute)
    )
    close = result.completed_minutes[0]
    assert close.minute_start == START.replace(second=0, microsecond=0)
    assert close.completed_at == first_next_minute.timestamp
    assert close.source_observation_timestamp == sample(45, "101").timestamp


def test_legacy_missing_poll_does_not_fabricate_bar_data() -> None:
    result = legacy_provider().replay(
        (sample(0, "100"), sample(120, "102"), sample(180, "103"))
    )
    assert [bar.minute_start.minute for bar in result.completed_minutes] == [30, 32]
    assert [bar.close for bar in result.completed_minutes] == [
        Decimal("100"),
        Decimal("102"),
    ]


def test_research_provider_has_distinct_non_exact_lineage() -> None:
    quote = ResearchMarketEvent(
        timestamp=START,
        kind=ResearchEventKind.QUOTE,
        bid=Decimal("99.99"),
        ask=Decimal("100.01"),
    )
    bar = ResearchMarketEvent(
        timestamp=START + timedelta(minutes=1),
        kind=ResearchEventKind.BAR,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("1200"),
    )
    result = ResearchReplayProvider(
        source_id="vendor-dataset-2026-08-28",
        source_kind="CANONICAL_HISTORICAL_QUOTES",
    ).ingest((quote, bar))
    assert result.lineage.replay_mode is ReplayMode.RESEARCH
    assert result.lineage.exact_prototype_replay is False
    assert result.lineage.source_id == "vendor-dataset-2026-08-28"
    assert result.events == (quote, bar)
    assert result.events[1].volume == Decimal("1200")


def test_ema_first_eight_values_unavailable() -> None:
    indicator = PrototypeEMA9()
    values = [indicator.append(Decimal(index)) for index in range(1, 9)]
    assert [value.ema for value in values] == [None] * 8


def test_ema_ninth_close_uses_sma_seed() -> None:
    indicator = PrototypeEMA9()
    ninth = [indicator.append(Decimal(index)) for index in range(1, 10)][-1]
    assert ninth.ema == Decimal("5")


def test_indicator_not_ready_at_close_nine() -> None:
    indicator = PrototypeEMA9()
    ninth = [indicator.append(Decimal(index)) for index in range(1, 10)][-1]
    assert ninth.ready is False
    assert indicator.ready is False


def test_indicator_ready_at_close_ten() -> None:
    indicator = PrototypeEMA9()
    tenth = [indicator.append(Decimal(index)) for index in range(1, 11)][-1]
    assert tenth.ready is True
    assert indicator.ready is True


def test_ema_recursive_decimal_value_matches_reference() -> None:
    indicator = PrototypeEMA9()
    tenth = [indicator.append(Decimal(index)) for index in range(1, 11)][-1]
    assert tenth.ema == Decimal("6.00")
