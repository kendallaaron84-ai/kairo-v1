from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.db.models.broker import BrokerAccount, BrokerInstrumentCapability
from app.db.models.configuration import Instrument, StrategyRegistry
from app.db.models.ledger import (
    BrokerCashSnapshot,
    Fill,
    KairoCapitalAuthorizationRecord,
    KairoOrder,
    MarketSnapshot,
    OrderIntent,
    OrderObservation,
    RiskDecision,
    SyntheticEvidenceManifest,
)
from app.db.models.projections import CapitalCell, CurrentPosition
from app.db.models.risk import (
    RiskGovernorState,
    RiskInstrumentMark,
    RiskSession,
    RiskStateEvent,
)
from app.domain.enums import OptionRight
from engine.execution.replay_orchestrator import (
    LegacyReplayInput,
    ReplayOptionCandidate,
    ReplayOptionChainEvent,
    ReplayOrchestrator,
    ReplaySessionConfig,
    ResearchReplayInput,
)
from engine.execution.virtual_clock import ReplayIdentityFactory
from engine.risk.models import FillAccountingEvent, PnLSnapshot
from engine.risk.pnl_tracker import apply_fill, realized_round_trip_pnl
from engine.strategy.ema_cross_strategy import (
    EMACrossStrategy,
    StrategyContract,
    StrategyPosition,
)
from engine.strategy.market_data import (
    LegacyReplayProvenance,
    LegacyReplayProvider,
    ReplayMode,
    ResearchEventKind,
    ResearchMarketEvent,
    ResearchReplayProvider,
    SampledPriceObservation,
)


pytestmark = pytest.mark.integration
EASTERN = ZoneInfo("America/New_York")
SESSION_OPEN = datetime(2026, 9, 1, 9, 30, tzinfo=EASTERN)
SESSION_CLOSE = datetime(2026, 9, 1, 16, 0, tzinfo=EASTERN)
BROKER_ID = UUID("30000000-0000-4000-8000-000000000001")
CELL_ID = UUID("30000000-0000-4000-8000-000000000002")
UNDERLYING_ID = UUID("30000000-0000-4000-8000-000000000003")
CALL_ID = UUID("30000000-0000-4000-8000-000000000004")
PUT_ID = UUID("30000000-0000-4000-8000-000000000005")
ALT_CALL_ID = UUID("30000000-0000-4000-8000-000000000008")


@dataclass
class SeededReplay:
    config: ReplaySessionConfig
    call: Instrument
    put: Instrument
    alt_call: Instrument


def seed_replay(
    session: Session, *, execution_authorized: bool = True
) -> SeededReplay:
    broker = BrokerAccount(
        broker_account_id=BROKER_ID,
        account_key="step4-paper",
        broker_name="PAPER_SIM_001",
        environment="PAPER",
        status="ACTIVE",
        effective_from=SESSION_OPEN,
    )
    underlying = Instrument(
        instrument_id=UNDERLYING_ID,
        symbol="TQQQ",
        asset_class="EQUITY",
        currency="USD",
        effective_from=SESSION_OPEN,
    )
    call = option_instrument(CALL_ID, "CALL", "10")
    put = option_instrument(PUT_ID, "PUT", "10")
    alt_call = option_instrument(ALT_CALL_ID, "CALL", "11")
    session.add_all([broker, underlying, call, put, alt_call])
    session.flush()
    strategy = session.get(StrategyRegistry, ("EMA-CROSS-001", "1.0.0"))
    assert strategy is not None
    session.add(
        CapitalCell(
            cell_id=CELL_ID,
            cell_code="STEP4-CELL",
            seed_capital=Decimal("1000"),
            status="ACTIVE",
            autonomy_tier="APPRENTICE",
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.version_tag,
            target_treasury_code="META",
            updated_at=SESSION_OPEN,
        )
    )
    session.flush()
    for instrument in (call, put, alt_call):
        session.add(
            BrokerInstrumentCapability(
                capability_id=UUID(int=instrument.instrument_id.int + 100),
                broker_account_id=BROKER_ID,
                instrument_id=instrument.instrument_id,
                can_trade=True,
                can_fractional=False,
                can_short=False,
                notional_orders_supported=False,
                options_supported=True,
                extended_hours_supported=False,
                minimum_quantity=Decimal("1"),
                effective_from=SESSION_OPEN,
            )
        )
    cash = BrokerCashSnapshot(
        snapshot_id=UUID("30000000-0000-4000-8000-000000000006"),
        broker_account_id=BROKER_ID,
        broker_cash=Decimal("1000"),
        settled_cash=Decimal("1000"),
        unsettled_cash=Decimal("0"),
        buying_power=Decimal("1000"),
        currency="USD",
        captured_at=SESSION_OPEN,
    )
    session.add(cash)
    session.flush()
    session.add(
        KairoCapitalAuthorizationRecord(
            authorization_id=UUID("30000000-0000-4000-8000-000000000007"),
            cell_id=CELL_ID,
            broker_snapshot_id=cash.snapshot_id,
            broker_account_id=BROKER_ID,
            settled_cash=Decimal("1000"),
            safety_reserve=Decimal("0"),
            ownership_treasury_reserved=Decimal("0"),
            replication_reserve=Decimal("0"),
            committed_obligations=Decimal("0"),
            authorized_trading_cash=Decimal("1000"),
            computed_at=SESSION_OPEN,
        )
    )
    session.flush()
    return SeededReplay(
        config=ReplaySessionConfig(
            session_id="STEP4-SESSION",
            cell_id=CELL_ID,
            broker_account_id=BROKER_ID,
            session_open=SESSION_OPEN,
            session_close=SESSION_CLOSE,
            execution_authorized_for_replay=execution_authorized,
            initial_cash_usd=Decimal("1000"),
        ),
        call=call,
        put=put,
        alt_call=alt_call,
    )


def option_instrument(
    instrument_id: UUID, right: str, strike: str
) -> Instrument:
    strike_code = f"{int(Decimal(strike) * 1000):08d}"
    return Instrument(
        instrument_id=instrument_id,
        symbol=f"TQQQ-{right}-{strike}",
        asset_class="OPTION",
        currency="USD",
        underlying_symbol="TQQQ",
        contract_symbol=f"TQQQ260901{right[0]}{strike_code}",
        expiration_date=date(2026, 9, 1),
        strike_price=Decimal(strike),
        option_right=right,
        contract_multiplier=Decimal("100"),
        listing_type="STANDARD",
        effective_from=SESSION_OPEN,
    )


def candidate(
    instrument: Instrument,
    *,
    bid: str = "0.47",
    ask: str = "0.50",
    volume: int = 10,
    open_interest: int = 0,
) -> ReplayOptionCandidate:
    return ReplayOptionCandidate(
        instrument_id=instrument.instrument_id,
        underlying_symbol=instrument.underlying_symbol,
        expiration_date=instrument.expiration_date,
        strike_price=instrument.strike_price,
        option_right=OptionRight(instrument.option_right),
        contract_symbol=instrument.contract_symbol,
        contract_multiplier=instrument.contract_multiplier,
        listing_type=instrument.listing_type,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=Decimal("10"),
        ask_size=Decimal("10"),
        volume=volume,
        open_interest=open_interest,
    )


def chain(
    seeded: SeededReplay,
    timestamp: datetime,
    *,
    call_bid: str = "0.47",
    call_ask: str = "0.50",
    candidates: tuple[ReplayOptionCandidate, ...] | None = None,
) -> ReplayOptionChainEvent:
    return ReplayOptionChainEvent(
        timestamp=timestamp,
        underlying_symbol="TQQQ",
        candidates=candidates
        or (
            candidate(seeded.call, bid=call_bid, ask=call_ask),
            candidate(seeded.put),
        ),
    )


def legacy_provider() -> LegacyReplayProvider:
    return LegacyReplayProvider(
        source_id="prototype-samples-step4",
        provenance=LegacyReplayProvenance.EXACT_OBSERVED_SAMPLES,
        instrument_id=UNDERLYING_ID,
        symbol="TQQQ",
    )


def legacy_observations() -> tuple[SampledPriceObservation, ...]:
    prices = [Decimal("10")] * 9 + [Decimal("11"), Decimal("11")]
    return tuple(
        SampledPriceObservation(
            timestamp=SESSION_OPEN + timedelta(minutes=index),
            price=price,
            instrument_id=UNDERLYING_ID,
            symbol="TQQQ",
        )
        for index, price in enumerate(prices)
    )


def legacy_input(
    seeded: SeededReplay,
    *,
    observations: tuple[SampledPriceObservation, ...] | None = None,
    chain_factory=None,
) -> LegacyReplayInput:
    source = observations or legacy_observations()
    make_chain = chain_factory or (lambda timestamp: chain(seeded, timestamp))
    return LegacyReplayInput(
        provider=legacy_provider(),
        observations=source,
        option_chains=tuple(make_chain(item.timestamp) for item in source),
    )


def research_input(seeded: SeededReplay) -> ResearchReplayInput:
    prices = [Decimal("10")] * 9 + [Decimal("11")]
    events = tuple(
        ResearchMarketEvent(
            timestamp=SESSION_OPEN + timedelta(minutes=index),
            kind=ResearchEventKind.BAR,
            instrument_id=UNDERLYING_ID,
            symbol="TQQQ",
            open=price,
            high=price,
            low=price,
            close=price,
            volume=Decimal("1000"),
        )
        for index, price in enumerate(prices)
    )
    return ResearchReplayInput(
        provider=ResearchReplayProvider(
            source_id="canonical-research-bars-step4",
            source_kind="CANONICAL_HISTORICAL_BARS",
            instrument_id=UNDERLYING_ID,
            symbol="TQQQ",
        ),
        events=events,
        option_chains=tuple(chain(seeded, item.timestamp) for item in events),
    )


def clean_replay_records(session: Session) -> None:
    # PostgreSQL TRUNCATE is transactional and intentionally bypasses the
    # immutable-ledger DELETE trigger for this isolated clean-database replay test.
    session.execute(text("TRUNCATE TABLE fill_realized_pnl"))
    for model in (
        Fill,
        OrderObservation,
        KairoOrder,
        RiskDecision,
        OrderIntent,
        CurrentPosition,
        RiskInstrumentMark,
        RiskStateEvent,
        RiskGovernorState,
        RiskSession,
        MarketSnapshot,
    ):
        session.execute(delete(model))
    session.flush()
    session.expire_all()


def run_replay(session: Session, seeded: SeededReplay):
    orchestrator = ReplayOrchestrator(session, seeded.config)
    return orchestrator, orchestrator.replay_legacy((legacy_input(seeded),))


def test_replay_rejects_timezone_naive_market_event(db_session: Session) -> None:
    seeded = seed_replay(db_session)
    invalid = SampledPriceObservation.model_construct(
        timestamp=datetime(2026, 9, 1, 9, 30),
        price=Decimal("10"),
        instrument_id=UNDERLYING_ID,
        symbol="TQQQ",
    )
    stream = LegacyReplayInput(provider=legacy_provider(), observations=(invalid,))
    with pytest.raises(ValueError, match="timezone-aware"):
        ReplayOrchestrator(db_session, seeded.config).replay_legacy((stream,))


def test_replay_generated_ids_are_deterministic(db_session: Session) -> None:
    seeded = seed_replay(db_session)
    _, first = run_replay(db_session, seeded)
    clean_replay_records(db_session)
    _, second = run_replay(db_session, seeded)
    assert first.financial_ids == second.financial_ids


def test_same_economic_replay_remains_identity_stable_when_nonfinancial_telemetry_is_added(
    db_session: Session,
) -> None:
    factory_a = ReplayIdentityFactory("stable-session")
    first_a = factory_a.generate_id("fill", CALL_ID, SESSION_OPEN, parent_id=CELL_ID)
    factory_a.generate_id("telemetry", CALL_ID, SESSION_OPEN, parent_id=CELL_ID)
    second_a = factory_a.generate_id("fill", CALL_ID, SESSION_OPEN, parent_id=CELL_ID)
    factory_b = ReplayIdentityFactory("stable-session")
    first_b = factory_b.generate_id("fill", CALL_ID, SESSION_OPEN, parent_id=CELL_ID)
    second_b = factory_b.generate_id("fill", CALL_ID, SESSION_OPEN, parent_id=CELL_ID)
    assert (first_a, second_a) == (first_b, second_b)


def test_replay_generated_timestamps_use_virtual_clock_only(db_session: Session) -> None:
    seeded = seed_replay(db_session)
    run_replay(db_session, seeded)
    allowed = {item.timestamp for item in legacy_observations()}
    timestamp_values = [
        *db_session.scalars(select(MarketSnapshot.captured_at)),
        *db_session.scalars(select(OrderIntent.created_at)),
        *db_session.scalars(select(RiskDecision.decided_at)),
        *db_session.scalars(select(KairoOrder.submitted_at)),
        *db_session.scalars(select(OrderObservation.observed_at)),
        *db_session.scalars(select(Fill.filled_at)),
        *db_session.scalars(select(RiskStateEvent.recorded_at)),
    ]
    assert timestamp_values
    assert set(timestamp_values).issubset(allowed)


def test_replay_manifest_is_stable_across_fresh_databases(db_session: Session) -> None:
    seeded = seed_replay(db_session)
    _, first = run_replay(db_session, seeded)
    clean_replay_records(db_session)
    _, second = run_replay(db_session, seeded)
    assert first.manifest_hash == second.manifest_hash


def test_replay_persists_existing_deterministic_manifest_as_synthetic_evidence(
    db_session: Session,
) -> None:
    seeded = seed_replay(db_session)
    _, result = run_replay(db_session, seeded)
    persisted = db_session.get(SyntheticEvidenceManifest, result.manifest_id)
    assert persisted is not None
    assert persisted.manifest_hash == result.manifest_hash
    assert persisted.manifest_type == "REPLAY_RUN"
    assert persisted.manifest_algorithm == "REPLAY-MANIFEST-v1"
    assert persisted.cell_id == seeded.config.cell_id


def test_identical_replay_produces_same_manifest_identity(db_session: Session) -> None:
    seeded = seed_replay(db_session)
    _, first = run_replay(db_session, seeded)
    clean_replay_records(db_session)
    _, second = run_replay(db_session, seeded)
    assert first.manifest_id == second.manifest_id


def test_manifest_hash_recomputes_from_canonical_replay_facts(db_session: Session) -> None:
    seeded = seed_replay(db_session)
    orchestrator, result = run_replay(db_session, seeded)
    recomputed, ids = orchestrator.build_manifest()
    persisted = db_session.get(SyntheticEvidenceManifest, result.manifest_id)
    assert persisted is not None
    assert recomputed == persisted.manifest_hash == result.manifest_hash
    assert persisted.source_count == len(ids)
    assert persisted.source_refs == {"financial_ids": [str(item) for item in ids]}


def test_slippage_is_not_double_counted_in_session_pnl(db_session: Session) -> None:
    updated = apply_fill(
        PnLSnapshot(net_pnl=Decimal("0")),
        FillAccountingEvent(
            fill_id=CALL_ID,
            kairo_order_id=CELL_ID,
            broker_account_id=BROKER_ID,
            instrument_id=CALL_ID,
            realized_pnl_delta_usd=Decimal("10"),
            commission_fees_usd=Decimal("1"),
            slippage_usd=Decimal("2"),
            fill_price=Decimal("1"),
            filled_qty=Decimal("1"),
            timestamp=SESSION_OPEN,
        ),
    )
    assert updated.slippage_usd == Decimal("2")
    assert updated.net_pnl == Decimal("9")


def test_round_trip_realized_pnl_matches_effective_fill_prices(
    db_session: Session,
) -> None:
    long_pnl = realized_round_trip_pnl(
        entry_price=Decimal("1.01"),
        exit_price=Decimal("1.10"),
        quantity=Decimal("2"),
        contract_multiplier=Decimal("100"),
        position_side="LONG",
    )
    short_pnl = realized_round_trip_pnl(
        entry_price=Decimal("1.10"),
        exit_price=Decimal("1.01"),
        quantity=Decimal("2"),
        contract_multiplier=Decimal("100"),
        position_side="SHORT",
    )
    assert long_pnl == short_pnl == Decimal("18.00")


def test_open_position_market_mark_can_trigger_loss_halt_without_new_fill(
    db_session: Session,
) -> None:
    seeded = seed_replay(db_session)
    orchestrator, _ = run_replay(db_session, seeded)
    fill_count = db_session.scalar(select(func.count()).select_from(Fill))
    timestamp = SESSION_OPEN + timedelta(minutes=11)
    observation = SampledPriceObservation(
        timestamp=timestamp,
        price=Decimal("11"),
        instrument_id=UNDERLYING_ID,
        symbol="TQQQ",
    )
    orchestrator.replay_legacy(
        (
            LegacyReplayInput(
                provider=legacy_provider(),
                observations=(observation,),
                option_chains=(chain(seeded, timestamp, call_bid="0.30"),),
            ),
        )
    )
    assert orchestrator.governor.current_state().operational_state == "HALTED_HARD"
    assert db_session.scalar(select(func.count()).select_from(Fill)) == fill_count


def test_strategy_runtime_matches_frozen_price_ema_cross_behavior(
    db_session: Session,
) -> None:
    strategy = EMACrossStrategy(settled_cash=Decimal("1000"))
    contract = StrategyContract(
        instrument_id=CALL_ID,
        underlying_symbol="TQQQ",
        option_right=OptionRight.CALL,
        bid=Decimal("0.47"),
        ask=Decimal("0.50"),
        contract_multiplier=Decimal("100"),
    )
    first_nine = [
        strategy.on_bar(
            symbol="TQQQ",
            close=Decimal("10"),
            timestamp=SESSION_OPEN + timedelta(minutes=index),
            call_contract=contract,
        )
        for index in range(9)
    ]
    tenth = strategy.on_bar(
        symbol="TQQQ",
        close=Decimal("11"),
        timestamp=SESSION_OPEN + timedelta(minutes=9),
        call_contract=contract,
    )
    assert first_nine == [None] * 9
    assert tenth is not None and tenth.option_right is OptionRight.CALL


def test_strategy_runtime_preserves_two_loss_session_halt(db_session: Session) -> None:
    strategy = EMACrossStrategy(settled_cash=Decimal("1000"))
    strategy.record_close("TQQQ", realized_pnl=Decimal("-1"))
    strategy.record_close("SQQQ", realized_pnl=Decimal("-1"))
    contract = StrategyContract(
        instrument_id=CALL_ID,
        underlying_symbol="TQQQ",
        option_right=OptionRight.CALL,
        bid=Decimal("0.47"),
        ask=Decimal("0.50"),
        contract_multiplier=Decimal("100"),
    )
    for index in range(9):
        strategy.on_bar(
            symbol="TQQQ",
            close=Decimal("10"),
            timestamp=SESSION_OPEN + timedelta(minutes=index),
            call_contract=contract,
        )
    signal = strategy.on_bar(
        symbol="TQQQ",
        close=Decimal("11"),
        timestamp=SESSION_OPEN + timedelta(minutes=9),
        call_contract=contract,
    )
    assert strategy.entries_halted is True
    assert signal is None


def test_1545_runtime_emits_exit_intent_without_bypassing_governor(
    db_session: Session,
) -> None:
    seeded = seed_replay(db_session)
    orchestrator = ReplayOrchestrator(db_session, seeded.config)
    orchestrator.initialize()
    contract = StrategyContract(
        instrument_id=CALL_ID,
        underlying_symbol="TQQQ",
        option_right=OptionRight.CALL,
        bid=Decimal("0.49"),
        ask=Decimal("0.50"),
        contract_multiplier=Decimal("100"),
    )
    for index in range(9):
        orchestrator.strategy.on_bar(
            symbol="TQQQ",
            close=Decimal("10"),
            timestamp=SESSION_OPEN + timedelta(minutes=index),
            call_contract=contract,
        )
    orchestrator.strategy.record_open(
        StrategyPosition(
            instrument_id=CALL_ID,
            underlying_symbol="TQQQ",
            option_right=OptionRight.CALL,
            quantity=Decimal("1"),
            entry_price=Decimal("0.50"),
            contract_multiplier=Decimal("100"),
        )
    )
    db_session.add(
        CurrentPosition(
            position_id=UUID("30000000-0000-4000-8000-000000000010"),
            cell_id=CELL_ID,
            broker_account_id=BROKER_ID,
            instrument_id=CALL_ID,
            quantity=Decimal("1"),
            average_price=Decimal("0.50"),
            updated_at=SESSION_OPEN,
        )
    )
    db_session.flush()
    timestamp = datetime(2026, 9, 1, 15, 45, tzinfo=EASTERN)
    bar = ResearchMarketEvent(
        timestamp=timestamp,
        kind=ResearchEventKind.BAR,
        instrument_id=UNDERLYING_ID,
        symbol="TQQQ",
        open=Decimal("10"),
        high=Decimal("10"),
        low=Decimal("10"),
        close=Decimal("10"),
    )
    orchestrator.replay_research(
        (
            ResearchReplayInput(
                provider=ResearchReplayProvider(
                    source_id="forced-flatten-test",
                    source_kind="BAR",
                    instrument_id=UNDERLYING_ID,
                    symbol="TQQQ",
                ),
                events=(bar,),
                option_chains=(chain(seeded, timestamp, call_bid="0.49"),),
            ),
        )
    )
    intent = db_session.scalar(select(OrderIntent).order_by(OrderIntent.created_at.desc()))
    decision = db_session.scalar(
        select(RiskDecision).where(RiskDecision.intent_id == intent.intent_id)
    )
    assert intent.order_purpose == "EMERGENCY_EXIT"
    assert decision.verdict == "AUTHORIZED"


def test_legacy_replay_pipeline_uses_exact_sampled_minute_rollover(
    db_session: Session,
) -> None:
    seeded = seed_replay(db_session)
    observations = (
        SampledPriceObservation(
            timestamp=SESSION_OPEN + timedelta(seconds=5),
            price=Decimal("10"),
            instrument_id=UNDERLYING_ID,
            symbol="TQQQ",
        ),
        SampledPriceObservation(
            timestamp=SESSION_OPEN + timedelta(seconds=45),
            price=Decimal("10.25"),
            instrument_id=UNDERLYING_ID,
            symbol="TQQQ",
        ),
        SampledPriceObservation(
            timestamp=SESSION_OPEN + timedelta(minutes=1, seconds=2),
            price=Decimal("10.50"),
            instrument_id=UNDERLYING_ID,
            symbol="TQQQ",
        ),
    )
    ReplayOrchestrator(db_session, seeded.config).replay_legacy(
        (legacy_input(seeded, observations=observations),)
    )
    closing_snapshot = db_session.scalar(
        select(MarketSnapshot).where(
            MarketSnapshot.captured_at == observations[-1].timestamp,
            MarketSnapshot.instrument_id == UNDERLYING_ID,
        )
    )
    assert closing_snapshot.payload["completed_close"] == "10.25"
    assert closing_snapshot.payload["exact_prototype_replay"] is True
    assert "open" not in closing_snapshot.payload


def test_research_replay_preserves_non_exact_lineage(db_session: Session) -> None:
    seeded = seed_replay(db_session)
    result = ReplayOrchestrator(db_session, seeded.config).replay_research(
        (research_input(seeded),)
    )
    assert result.lineage[0].replay_mode is ReplayMode.RESEARCH
    assert result.lineage[0].exact_prototype_replay is False
    snapshot = db_session.scalar(
        select(MarketSnapshot).where(MarketSnapshot.instrument_id == UNDERLYING_ID)
    )
    assert snapshot.payload["exact_prototype_replay"] is False


def test_replay_option_selection_uses_frozen_filter_first_resolver(
    db_session: Session,
) -> None:
    seeded = seed_replay(db_session)

    def filter_first_chain(timestamp: datetime) -> ReplayOptionChainEvent:
        return chain(
            seeded,
            timestamp,
            candidates=(
                candidate(seeded.call, bid="0.57", ask="0.60"),
                candidate(seeded.alt_call),
                candidate(seeded.put),
            ),
        )

    ReplayOrchestrator(db_session, seeded.config).replay_legacy(
        (legacy_input(seeded, chain_factory=filter_first_chain),)
    )
    intent = db_session.scalar(select(OrderIntent))
    assert intent.instrument_id == ALT_CALL_ID


def test_replay_rejects_preselected_contract_that_fails_frozen_filters(
    db_session: Session,
) -> None:
    seeded = seed_replay(db_session)

    def ineligible_chain(timestamp: datetime) -> ReplayOptionChainEvent:
        return chain(
            seeded,
            timestamp,
            candidates=(
                candidate(seeded.call, bid="0.57", ask="0.60"),
                candidate(seeded.put),
            ),
        )

    ReplayOrchestrator(db_session, seeded.config).replay_legacy(
        (legacy_input(seeded, chain_factory=ineligible_chain),)
    )
    assert db_session.scalar(select(func.count()).select_from(OrderIntent)) == 0


def test_long_option_mark_to_market_uses_bid_not_midpoint(db_session: Session) -> None:
    seeded = seed_replay(db_session)
    orchestrator, _ = run_replay(db_session, seeded)
    timestamp = SESSION_OPEN + timedelta(minutes=11)
    observation = SampledPriceObservation(
        timestamp=timestamp,
        price=Decimal("11"),
        instrument_id=UNDERLYING_ID,
        symbol="TQQQ",
    )
    orchestrator.replay_legacy(
        (
            LegacyReplayInput(
                provider=legacy_provider(),
                observations=(observation,),
                option_chains=(
                    chain(
                        seeded,
                        timestamp,
                        call_bid="0.48",
                        call_ask="0.52",
                    ),
                ),
            ),
        )
    )
    state = orchestrator.governor.current_state()
    assert state.session_unrealized_pnl == Decimal("-6")
    assert state.operational_state == "HALTED_HARD"


def test_replay_requires_explicit_arm_authorization(db_session: Session) -> None:
    seeded = seed_replay(db_session, execution_authorized=False)
    orchestrator = ReplayOrchestrator(db_session, seeded.config)
    orchestrator.initialize()
    assert orchestrator.governor.current_state().operational_state == "DISARMED"
