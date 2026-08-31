from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, func, select
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
    ReplayMarketEvent,
    ReplayOptionQuote,
    ReplayOrchestrator,
    ReplaySessionConfig,
)
from engine.execution.virtual_clock import ReplayIdentityFactory
from engine.risk.models import FillAccountingEvent, PnLSnapshot
from engine.risk.pnl_tracker import apply_fill, realized_round_trip_pnl
from engine.strategy.ema_cross_strategy import (
    EMACrossStrategy,
    StrategyContract,
    StrategyPosition,
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


@dataclass
class SeededReplay:
    config: ReplaySessionConfig
    call: Instrument
    put: Instrument


def seed_replay(session: Session) -> SeededReplay:
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
    call = Instrument(
        instrument_id=CALL_ID,
        symbol="TQQQ-CALL-STEP4",
        asset_class="OPTION",
        currency="USD",
        underlying_symbol="TQQQ",
        contract_symbol="TQQQ260901C00050000",
        expiration_date=date(2026, 9, 1),
        strike_price=Decimal("50"),
        option_right="CALL",
        contract_multiplier=Decimal("100"),
        listing_type="STANDARD",
        effective_from=SESSION_OPEN,
    )
    put = Instrument(
        instrument_id=PUT_ID,
        symbol="TQQQ-PUT-STEP4",
        asset_class="OPTION",
        currency="USD",
        underlying_symbol="TQQQ",
        contract_symbol="TQQQ260901P00050000",
        expiration_date=date(2026, 9, 1),
        strike_price=Decimal("50"),
        option_right="PUT",
        contract_multiplier=Decimal("100"),
        listing_type="STANDARD",
        effective_from=SESSION_OPEN,
    )
    session.add_all([broker, underlying, call, put])
    session.flush()
    strategy = session.get(StrategyRegistry, ("EMA-CROSS-001", "1.0.0"))
    assert strategy is not None
    cell = CapitalCell(
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
    session.add(cell)
    session.flush()
    for instrument in (call, put):
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
            initial_cash_usd=Decimal("1000"),
        ),
        call=call,
        put=put,
    )


def option_quote(instrument_id: UUID, right: OptionRight, *, bid: str = "0.47"):
    return ReplayOptionQuote(
        instrument_id=instrument_id,
        option_right=right,
        bid=Decimal(bid),
        ask=Decimal("0.50"),
        bid_size=Decimal("10"),
        ask_size=Decimal("10"),
    )


def crossover_events() -> tuple[ReplayMarketEvent, ...]:
    prices = [Decimal("10")] * 9 + [Decimal("11")]
    return tuple(
        ReplayMarketEvent(
            instrument_id=UNDERLYING_ID,
            symbol="TQQQ",
            timestamp=SESSION_OPEN + timedelta(minutes=index),
            price=price,
            call_quote=option_quote(CALL_ID, OptionRight.CALL),
            put_quote=option_quote(PUT_ID, OptionRight.PUT),
        )
        for index, price in enumerate(prices)
    )


def clean_replay_records(session: Session) -> None:
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
    return orchestrator, orchestrator.replay(crossover_events())


def test_replay_rejects_timezone_naive_market_event(db_session: Session) -> None:
    seeded = seed_replay(db_session)
    orchestrator = ReplayOrchestrator(db_session, seeded.config)
    event = crossover_events()[0].model_copy(
        update={"timestamp": datetime(2026, 9, 1, 9, 30)}
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        orchestrator.process_event(event)


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
    allowed = {SESSION_OPEN, *(event.timestamp for event in crossover_events())}
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


def test_slippage_is_not_double_counted_in_session_pnl(db_session: Session) -> None:
    snapshot = PnLSnapshot(net_pnl=Decimal("0"))
    updated = apply_fill(
        snapshot,
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
    loss_event = ReplayMarketEvent(
        instrument_id=UNDERLYING_ID,
        symbol="TQQQ",
        timestamp=SESSION_OPEN + timedelta(minutes=10),
        price=Decimal("11"),
        call_quote=option_quote(CALL_ID, OptionRight.CALL, bid="0.30"),
        put_quote=option_quote(PUT_ID, OptionRight.PUT),
    )
    orchestrator.process_event(loss_event)
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
    orchestrator.process_event(
        ReplayMarketEvent(
            instrument_id=UNDERLYING_ID,
            symbol="TQQQ",
            timestamp=datetime(2026, 9, 1, 15, 45, tzinfo=EASTERN),
            price=Decimal("10"),
            call_quote=option_quote(CALL_ID, OptionRight.CALL, bid="0.49"),
            put_quote=option_quote(PUT_ID, OptionRight.PUT),
        )
    )
    intent = db_session.scalar(select(OrderIntent).order_by(OrderIntent.created_at.desc()))
    decision = db_session.scalar(
        select(RiskDecision).where(RiskDecision.intent_id == intent.intent_id)
    )
    assert intent.order_purpose == "EMERGENCY_EXIT"
    assert decision.verdict == "AUTHORIZED"
    assert db_session.scalar(
        select(func.count()).select_from(KairoOrder).where(
            KairoOrder.intent_id == intent.intent_id
        )
    ) == 1
