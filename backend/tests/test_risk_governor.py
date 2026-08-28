from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.broker import BrokerAccount, BrokerInstrumentCapability
from app.db.models.configuration import Instrument, StrategyRegistry
from app.db.models.ledger import (
    BrokerCashSnapshot,
    KairoCapitalAuthorizationRecord,
    OrderIntent,
    RiskDecision,
)
from app.db.models.projections import CapitalCell, CurrentPosition
from app.db.models.risk import RiskStateEvent
from engine.risk.commands import CancelOrderCommand, EmergencyExitCommand
from engine.risk.exceptions import InvalidStateTransition
from engine.risk.governor import RiskGovernor
from engine.risk.models import (
    BrokerCapabilityProfile,
    DecisionVerdict,
    DisqualificationReason,
    ExecutionEnvironment,
    FillAccountingEvent,
    InstrumentRiskProfile,
    IntentEvaluationInput,
    MarketMark,
    OperationalState,
    PendingRiskOrder,
    PositionSnapshot,
    RiskClassification,
    RiskEvaluationRequest,
    RiskSessionSpec,
    StrategyClearance,
)


pytestmark = pytest.mark.integration


@dataclass
class SeededContext:
    broker: BrokerAccount
    instrument: Instrument
    strategy: StrategyRegistry
    cell: CapitalCell
    capability: BrokerInstrumentCapability
    position: CurrentPosition | None


def initialize_governor(session: Session, *, armed: bool = True) -> RiskGovernor:
    governor = RiskGovernor(session)
    now = datetime.now(UTC)
    governor.initialize_session(
        RiskSessionSpec(
            session_id=f"session-{uuid4()}",
            trading_date=date.today(),
            session_open=now - timedelta(hours=1),
            session_close=now + timedelta(hours=6),
        )
    )
    if armed:
        governor.arm(authorized_cash_usd=Decimal("1000"))
    return governor


def seed_context(
    session: Session,
    *,
    asset_class: str = "EQUITY",
    multiplier: Decimal | None = None,
    position_quantity: Decimal | None = None,
    options_supported: bool = True,
    notional_supported: bool = True,
    can_fractional: bool = True,
    strategy_clearance: str = "LIVE",
    seed_capital: Decimal = Decimal("1000"),
) -> SeededContext:
    broker = BrokerAccount(
        broker_account_id=uuid4(),
        account_key=f"paper-{uuid4()}",
        broker_name="TEST",
        environment="PAPER",
        status="ACTIVE",
    )
    instrument_kwargs: dict = {}
    if asset_class == "OPTION":
        instrument_kwargs = {
            "underlying_symbol": "TQQQ",
            "contract_symbol": f"TQQQ{uuid4().hex[:16]}",
            "expiration_date": date(2026, 8, 28),
            "strike_price": Decimal("75"),
            "option_right": "CALL",
            "contract_multiplier": multiplier or Decimal("100"),
            "listing_type": "STANDARD",
        }
    instrument = Instrument(
        instrument_id=uuid4(),
        symbol=f"I{uuid4().hex[:10]}",
        asset_class=asset_class,
        currency="USD",
        **instrument_kwargs,
    )
    strategy = StrategyRegistry(
        strategy_id=f"STRAT-{uuid4().hex[:8]}",
        version_tag="1.0.0",
        display_name="Risk test strategy",
        status="ACTIVE",
        configuration={"clearance": strategy_clearance},
    )
    cell = CapitalCell(
        cell_id=uuid4(),
        cell_code=f"CELL-{uuid4().hex[:8]}",
        seed_capital=seed_capital,
        status="APPRENTICE",
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag,
        target_treasury_code="META",
    )
    session.add_all([broker, instrument, strategy])
    session.flush()
    session.add(cell)
    session.flush()
    capability = BrokerInstrumentCapability(
        capability_id=uuid4(),
        broker_account_id=broker.broker_account_id,
        instrument_id=instrument.instrument_id,
        can_trade=True,
        can_fractional=can_fractional,
        can_short=False,
        notional_orders_supported=notional_supported,
        options_supported=options_supported,
        extended_hours_supported=False,
        minimum_quantity=Decimal("0.01"),
    )
    session.add(capability)
    position = None
    if position_quantity is not None:
        position = CurrentPosition(
            position_id=uuid4(),
            cell_id=cell.cell_id,
            broker_account_id=broker.broker_account_id,
            instrument_id=instrument.instrument_id,
            quantity=position_quantity,
            average_price=Decimal("10"),
        )
        session.add(position)
    session.flush()
    return SeededContext(broker, instrument, strategy, cell, capability, position)


def make_request(
    session: Session,
    context: SeededContext,
    *,
    purpose: str = "ENTRY",
    side: str = "BUY",
    quantity: Decimal | None = Decimal("1"),
    notional: Decimal | None = None,
    mark_price: Decimal = Decimal("10"),
    age_seconds: Decimal = Decimal("0.2"),
    authorized_cash: Decimal = Decimal("1000"),
    authorized_exposure: Decimal = Decimal("1000"),
    strategy_clearance: StrategyClearance = StrategyClearance.LIVE,
    execution_environment: ExecutionEnvironment = ExecutionEnvironment.PAPER,
    capability_overrides: dict | None = None,
    position_override: PositionSnapshot | None | object = ...,
) -> RiskEvaluationRequest:
    intent_id = uuid4()
    intent = OrderIntent(
        intent_id=intent_id,
        cell_id=context.cell.cell_id,
        strategy_id=context.strategy.strategy_id,
        strategy_version=context.strategy.version_tag,
        instrument_id=context.instrument.instrument_id,
        client_order_key=f"risk-{uuid4()}",
        order_purpose=purpose,
        side=side,
        target_quantity=quantity,
        target_notional_usd=notional,
        order_type="MARKET",
    )
    session.add(intent)
    session.flush()
    now = datetime.now(UTC)
    cash_snapshot = BrokerCashSnapshot(
        snapshot_id=uuid4(),
        broker_account_id=context.broker.broker_account_id,
        broker_cash=authorized_cash,
        settled_cash=authorized_cash,
        unsettled_cash=Decimal("0"),
        buying_power=authorized_cash,
        currency="USD",
        captured_at=now,
    )
    session.add(cash_snapshot)
    session.flush()
    session.add(
        KairoCapitalAuthorizationRecord(
            authorization_id=uuid4(),
            cell_id=context.cell.cell_id,
            broker_snapshot_id=cash_snapshot.snapshot_id,
            broker_account_id=context.broker.broker_account_id,
            settled_cash=authorized_cash,
            safety_reserve=Decimal("0"),
            ownership_treasury_reserved=Decimal("0"),
            replication_reserve=Decimal("0"),
            committed_obligations=Decimal("0"),
            authorized_trading_cash=authorized_cash,
            computed_at=now,
        )
    )
    session.flush()
    capability_values = {
        "broker_account_id": context.broker.broker_account_id,
        "instrument_id": context.instrument.instrument_id,
        "can_trade": context.capability.can_trade,
        "can_fractional": context.capability.can_fractional,
        "can_short": context.capability.can_short,
        "notional_orders_supported": context.capability.notional_orders_supported,
        "options_supported": context.capability.options_supported,
        "extended_hours_supported": context.capability.extended_hours_supported,
        "minimum_quantity": context.capability.minimum_quantity,
    }
    capability_values.update(capability_overrides or {})
    if position_override is ...:
        position = (
            PositionSnapshot(
                position_id=context.position.position_id,
                cell_id=context.position.cell_id,
                broker_account_id=context.position.broker_account_id,
                instrument_id=context.position.instrument_id,
                quantity=context.position.quantity,
                average_price=context.position.average_price,
                contract_multiplier=context.instrument.contract_multiplier or Decimal("1"),
            )
            if context.position is not None
            else None
        )
    else:
        position = position_override
    return RiskEvaluationRequest(
        intent=IntentEvaluationInput(
            intent_id=intent_id,
            cell_id=context.cell.cell_id,
            strategy_id=context.strategy.strategy_id,
            strategy_version=context.strategy.version_tag,
            instrument_id=context.instrument.instrument_id,
            order_purpose=purpose,
            side=side,
            target_quantity=quantity,
            target_notional_usd=notional,
            order_type="MARKET",
        ),
        broker_account_id=context.broker.broker_account_id,
        instrument=InstrumentRiskProfile(
            instrument_id=context.instrument.instrument_id,
            asset_class=context.instrument.asset_class,
            contract_multiplier=context.instrument.contract_multiplier,
        ),
        capability=BrokerCapabilityProfile(**capability_values),
        current_position=position,
        market_mark=MarketMark(
            instrument_id=context.instrument.instrument_id,
            mark_price=mark_price,
            source_timestamp=now - timedelta(seconds=float(age_seconds)),
            received_at=now,
        ),
        strategy_clearance=strategy_clearance,
        execution_environment=execution_environment,
        authorized_trading_cash=authorized_cash,
        authorized_exposure_usd=authorized_exposure,
        current_exposure_usd=(abs(context.position.quantity) * mark_price if context.position else Decimal("0")),
    )


def accounting_event(realized: str, *, fees: str = "0", slippage: str = "0") -> FillAccountingEvent:
    return FillAccountingEvent(
        fill_id=uuid4(),
        kairo_order_id=uuid4(),
        broker_account_id=uuid4(),
        instrument_id=uuid4(),
        realized_pnl_delta_usd=Decimal(realized),
        commission_fees_usd=Decimal(fees),
        slippage_usd=Decimal(slippage),
        fill_price=Decimal("10"),
        filled_qty=Decimal("1"),
        timestamp=datetime.now(UTC),
    )


def position_snapshot(context: SeededContext) -> PositionSnapshot:
    assert context.position is not None
    return PositionSnapshot(
        position_id=context.position.position_id,
        cell_id=context.position.cell_id,
        broker_account_id=context.position.broker_account_id,
        instrument_id=context.position.instrument_id,
        quantity=context.position.quantity,
        average_price=context.position.average_price,
        contract_multiplier=context.instrument.contract_multiplier or Decimal("1"),
    )


def test_emergency_exit_is_allowed_while_halted(db_session: Session) -> None:
    context = seed_context(db_session, position_quantity=Decimal("2"))
    governor = initialize_governor(db_session)
    governor.flatten_all(
        authorized_cash_usd=Decimal("0"),
        open_positions=[position_snapshot(context)],
        pending_orders=[],
    )
    result = governor.evaluate(
        make_request(
            db_session, context, purpose="EMERGENCY_EXIT", side="SELL", quantity=Decimal("2")
        )
    )
    assert result.verdict is DecisionVerdict.AUTHORIZED


@pytest.mark.parametrize("purpose", ["TAKE_PROFIT", "STOP_LOSS"])
def test_locked_for_day_allows_risk_reducing_exit(
    db_session: Session, purpose: str
) -> None:
    context = seed_context(db_session, position_quantity=Decimal("2"))
    governor = initialize_governor(db_session)
    governor.record_fill_accounting(
        accounting_event("20"), authorized_cash_usd=Decimal("0")
    )
    result = governor.evaluate(
        make_request(db_session, context, purpose=purpose, side="SELL", quantity=Decimal("1"))
    )
    assert result.verdict is DecisionVerdict.AUTHORIZED


def test_stale_data_blocks_entry_but_not_emergency_exit(db_session: Session) -> None:
    entry_context = seed_context(db_session)
    governor = initialize_governor(db_session)
    entry = governor.evaluate(make_request(db_session, entry_context, age_seconds=Decimal("2")))
    assert entry.reason is DisqualificationReason.MARKET_DATA_STALE

    exit_context = seed_context(db_session, position_quantity=Decimal("1"))
    exit_result = governor.evaluate(
        make_request(
            db_session,
            exit_context,
            purpose="EMERGENCY_EXIT",
            side="SELL",
            age_seconds=Decimal("20"),
        )
    )
    assert exit_result.verdict is DecisionVerdict.AUTHORIZED


def test_exit_does_not_require_available_atc(db_session: Session) -> None:
    context = seed_context(db_session, position_quantity=Decimal("1"))
    governor = initialize_governor(db_session, armed=False)
    result = governor.evaluate(
        make_request(
            db_session,
            context,
            purpose="STOP_LOSS",
            side="SELL",
            authorized_cash=Decimal("0"),
        )
    )
    assert result.verdict is DecisionVerdict.AUTHORIZED


def test_exit_reduces_projected_cell_exposure(db_session: Session) -> None:
    context = seed_context(db_session, position_quantity=Decimal("10"))
    governor = initialize_governor(db_session)
    result = governor.evaluate(
        make_request(
            db_session,
            context,
            purpose="TAKE_PROFIT",
            side="SELL",
            quantity=Decimal("5"),
            authorized_exposure=Decimal("50"),
        )
    )
    assert result.verdict is DecisionVerdict.AUTHORIZED
    assert result.metrics.projected_exposure_usd == Decimal("50")


def test_service_restart_does_not_clear_daily_halt(db_session: Session) -> None:
    governor = initialize_governor(db_session)
    governor.record_fill_accounting(
        accounting_event("-6"), authorized_cash_usd=Decimal("0")
    )
    db_session.flush()
    db_session.expire_all()
    restarted = RiskGovernor(db_session)
    assert restarted.current_state().operational_state == OperationalState.HALTED_HARD.value


def test_new_session_does_not_auto_arm(db_session: Session) -> None:
    governor = initialize_governor(db_session, armed=False)
    assert governor.current_state().operational_state == OperationalState.DISARMED.value


def test_manual_pause_blocks_entries_but_preserves_exit_authority(db_session: Session) -> None:
    entry_context = seed_context(db_session)
    exit_context = seed_context(db_session, position_quantity=Decimal("1"))
    governor = initialize_governor(db_session)
    governor.halt_trading(authorized_cash_usd=Decimal("100"))
    entry = governor.evaluate(make_request(db_session, entry_context))
    exit_result = governor.evaluate(
        make_request(db_session, exit_context, purpose="STOP_LOSS", side="SELL")
    )
    assert entry.reason is DisqualificationReason.NOT_ARMED
    assert exit_result.verdict is DecisionVerdict.AUTHORIZED


def test_flatten_all_emits_exit_intents_without_claiming_broker_fill(
    db_session: Session,
) -> None:
    context = seed_context(db_session, position_quantity=Decimal("3"))
    governor = initialize_governor(db_session)
    pending = PendingRiskOrder(
        kairo_order_id=uuid4(),
        intent_id=uuid4(),
        broker_account_id=context.broker.broker_account_id,
        classification=RiskClassification.RISK_INCREASING,
    )
    commands = governor.flatten_all(
        authorized_cash_usd=Decimal("0"),
        open_positions=[position_snapshot(context)],
        pending_orders=[pending],
    )
    assert any(isinstance(command, CancelOrderCommand) for command in commands)
    assert any(isinstance(command, EmergencyExitCommand) for command in commands)
    db_session.refresh(context.position)
    assert context.position.quantity == Decimal("3")


def test_option_cash_cost_uses_premium_times_contract_multiplier(db_session: Session) -> None:
    context = seed_context(db_session, asset_class="OPTION", multiplier=Decimal("100"))
    governor = initialize_governor(db_session)
    result = governor.evaluate(
        make_request(db_session, context, quantity=Decimal("1"), mark_price=Decimal("0.42"))
    )
    assert result.verdict is DecisionVerdict.AUTHORIZED
    assert result.metrics.requested_cash_usd == Decimal("42.00")


def test_option_notional_sizing_is_rejected(db_session: Session) -> None:
    context = seed_context(db_session, asset_class="OPTION")
    governor = initialize_governor(db_session)
    result = governor.evaluate(
        make_request(db_session, context, quantity=None, notional=Decimal("42"))
    )
    assert result.reason is DisqualificationReason.OPTION_NOTIONAL_SIZING_PROHIBITED


def test_non_standard_contract_multiplier_arithmetic(db_session: Session) -> None:
    context = seed_context(db_session, asset_class="OPTION", multiplier=Decimal("10"))
    governor = initialize_governor(db_session)
    result = governor.evaluate(
        make_request(db_session, context, quantity=Decimal("2"), mark_price=Decimal("0.42"))
    )
    assert result.metrics.requested_cash_usd == Decimal("8.40")


def test_governor_uses_source_timestamp_to_determine_staleness(db_session: Session) -> None:
    context = seed_context(db_session)
    governor = initialize_governor(db_session)
    result = governor.evaluate(
        make_request(db_session, context, age_seconds=Decimal("1.5001"))
    )
    assert result.reason is DisqualificationReason.MARKET_DATA_STALE


def test_immutable_risk_decision_logging(db_session: Session) -> None:
    context = seed_context(db_session)
    governor = initialize_governor(db_session)
    authorized = governor.evaluate(make_request(db_session, context))
    rejected = governor.evaluate(
        make_request(db_session, context, authorized_cash=Decimal("0"))
    )
    decisions = list(db_session.scalars(select(RiskDecision)))
    assert {decision.decision_id for decision in decisions} == {
        authorized.decision_id,
        rejected.decision_id,
    }
    assert {decision.verdict for decision in decisions} == {"AUTHORIZED", "REJECTED"}


def test_duplicate_circuit_breaker_events_do_not_duplicate_emergency_exits(
    db_session: Session,
) -> None:
    context = seed_context(db_session, position_quantity=Decimal("1"))
    governor = initialize_governor(db_session)
    first = governor.record_fill_accounting(
        accounting_event("-6"),
        authorized_cash_usd=Decimal("0"),
        open_positions=[position_snapshot(context)],
    )
    second = governor.record_fill_accounting(
        accounting_event("0"),
        authorized_cash_usd=Decimal("0"),
        open_positions=[position_snapshot(context)],
    )
    assert len([command for command in first if isinstance(command, EmergencyExitCommand)]) == 1
    assert second == ()
    hard_events = db_session.scalar(
        select(func.count()).select_from(RiskStateEvent).where(
            RiskStateEvent.new_state == OperationalState.HALTED_HARD.value
        )
    )
    assert hard_events == 1


@pytest.mark.parametrize(
    ("quantity", "expected_reason"),
    [
        (Decimal("2"), DisqualificationReason.EXIT_EXCEEDS_POSITION_QTY),
        (Decimal("3"), DisqualificationReason.EXIT_WOULD_INCREASE_RISK),
    ],
)
def test_exit_quantity_cannot_exceed_or_reverse_position(
    db_session: Session,
    quantity: Decimal,
    expected_reason: DisqualificationReason,
) -> None:
    context = seed_context(db_session, position_quantity=Decimal("1"))
    governor = initialize_governor(db_session)
    result = governor.evaluate(
        make_request(
            db_session, context, purpose="TAKE_PROFIT", side="SELL", quantity=quantity
        )
    )
    assert result.reason is expected_reason


def test_fake_take_profit_label_cannot_conceal_risk_increase(db_session: Session) -> None:
    context = seed_context(db_session, position_quantity=Decimal("1"))
    governor = initialize_governor(db_session)
    result = governor.evaluate(
        make_request(db_session, context, purpose="TAKE_PROFIT", side="BUY")
    )
    assert result.reason is DisqualificationReason.EXIT_WOULD_INCREASE_RISK


def test_paper_only_strategy_cannot_request_live_capital(db_session: Session) -> None:
    context = seed_context(db_session, strategy_clearance="PAPER_ONLY")
    governor = initialize_governor(db_session)
    result = governor.evaluate(
        make_request(
            db_session,
            context,
            strategy_clearance=StrategyClearance.PAPER_ONLY,
            execution_environment=ExecutionEnvironment.LIVE,
        )
    )
    assert result.reason is DisqualificationReason.STRATEGY_CLEARANCE_MISMATCH


def test_unsupported_option_capability_fails_closed(db_session: Session) -> None:
    context = seed_context(db_session, asset_class="OPTION", options_supported=False)
    governor = initialize_governor(db_session)
    result = governor.evaluate(make_request(db_session, context, mark_price=Decimal("0.42")))
    assert result.reason is DisqualificationReason.BROKER_CAPABILITY_UNSUPPORTED


def test_unsupported_notional_capability_fails_closed(db_session: Session) -> None:
    context = seed_context(db_session, notional_supported=False)
    governor = initialize_governor(db_session)
    result = governor.evaluate(
        make_request(db_session, context, quantity=None, notional=Decimal("10"))
    )
    assert result.reason is DisqualificationReason.BROKER_CAPABILITY_UNSUPPORTED


def test_future_invalid_market_timestamp_fails_closed_for_entries(db_session: Session) -> None:
    context = seed_context(db_session)
    governor = initialize_governor(db_session)
    result = governor.evaluate(
        make_request(db_session, context, age_seconds=Decimal("-0.1"))
    )
    assert result.reason is DisqualificationReason.INVALID_MARKET_TIMESTAMP


def test_restart_preserves_locked_for_day(db_session: Session) -> None:
    governor = initialize_governor(db_session)
    governor.record_fill_accounting(
        accounting_event("20"), authorized_cash_usd=Decimal("0")
    )
    db_session.expire_all()
    assert RiskGovernor(db_session).current_state().operational_state == "LOCKED_FOR_DAY"


def test_restart_preserves_session_pnl_components(db_session: Session) -> None:
    governor = initialize_governor(db_session)
    governor.record_fill_accounting(
        accounting_event("10", fees="1", slippage="0.5"),
        authorized_cash_usd=Decimal("100"),
    )
    db_session.expire_all()
    state = RiskGovernor(db_session).current_state()
    assert state.session_realized_pnl == Decimal("10")
    assert state.session_fees_usd == Decimal("1")
    assert state.session_slippage_usd == Decimal("0.5")
    assert state.session_net_pnl == Decimal("8.5")


def test_halted_hard_cannot_be_manually_rearmed(db_session: Session) -> None:
    governor = initialize_governor(db_session)
    governor.record_fill_accounting(
        accounting_event("-6"), authorized_cash_usd=Decimal("0")
    )
    with pytest.raises(InvalidStateTransition):
        governor.arm(authorized_cash_usd=Decimal("100"))


def test_flat_locked_cannot_create_new_exposure(db_session: Session) -> None:
    context = seed_context(db_session)
    governor = initialize_governor(db_session)
    governor.record_fill_accounting(
        accounting_event("20"), authorized_cash_usd=Decimal("0")
    )
    governor.reconcile_confirmed_positions(
        open_positions=[], authorized_cash_usd=Decimal("0")
    )
    result = governor.evaluate(make_request(db_session, context))
    assert result.reason is DisqualificationReason.SYSTEM_HALTED


def test_market_mark_triggers_hard_loss_immediately(db_session: Session) -> None:
    context = seed_context(db_session, position_quantity=Decimal("1"))
    governor = initialize_governor(db_session)
    now = datetime.now(UTC)
    commands = governor.record_market_mark(
        MarketMark(
            instrument_id=context.instrument.instrument_id,
            mark_price=Decimal("4"),
            source_timestamp=now,
            received_at=now,
        ),
        positions=[position_snapshot(context)],
        authorized_cash_usd=Decimal("0"),
    )
    assert governor.current_state().operational_state == "HALTED_HARD"
    assert any(isinstance(command, EmergencyExitCommand) for command in commands)


def test_entry_cannot_exceed_persisted_cell_exposure_authority(
    db_session: Session,
) -> None:
    context = seed_context(db_session, seed_capital=Decimal("50"))
    governor = initialize_governor(db_session)
    result = governor.evaluate(
        make_request(
            db_session,
            context,
            quantity=Decimal("6"),
            authorized_exposure=Decimal("1000000"),
        )
    )
    assert result.reason is DisqualificationReason.CELL_EXPOSURE_EXCEEDED


def test_entry_cannot_exceed_persisted_capital_authorization(
    db_session: Session,
) -> None:
    context = seed_context(db_session)
    governor = initialize_governor(db_session)
    result = governor.evaluate(
        make_request(db_session, context, authorized_cash=Decimal("5"))
    )
    assert result.reason is DisqualificationReason.INSUFFICIENT_AUTHORIZED_CASH


def test_caller_capability_claim_cannot_override_broker_capability(
    db_session: Session,
) -> None:
    context = seed_context(db_session, notional_supported=False)
    governor = initialize_governor(db_session)
    result = governor.evaluate(
        make_request(
            db_session,
            context,
            quantity=None,
            notional=Decimal("10"),
            capability_overrides={"notional_orders_supported": True},
        )
    )
    assert result.reason is DisqualificationReason.BROKER_CAPABILITY_UNSUPPORTED


def test_caller_intent_claim_cannot_override_persisted_order_intent(
    db_session: Session,
) -> None:
    context = seed_context(db_session)
    governor = initialize_governor(db_session)
    request = make_request(db_session, context, quantity=Decimal("1"))
    forged = request.model_copy(
        update={
            "intent": request.intent.model_copy(
                update={"target_quantity": Decimal("1000000")}
            )
        }
    )
    result = governor.evaluate(forged)
    assert result.verdict is DecisionVerdict.AUTHORIZED
    assert result.metrics.requested_quantity == Decimal("1")


def test_new_session_resets_prior_armed_state_and_pnl(db_session: Session) -> None:
    governor = initialize_governor(db_session)
    first_session_id = governor.current_state().current_session_id
    governor.record_fill_accounting(
        accounting_event("3"), authorized_cash_usd=Decimal("100")
    )
    now = datetime.now(UTC)
    next_session = governor.initialize_session(
        RiskSessionSpec(
            session_id=f"session-{uuid4()}",
            trading_date=date.today() + timedelta(days=1),
            session_open=now + timedelta(days=1),
            session_close=now + timedelta(days=1, hours=6),
        )
    )
    assert next_session.current_session_id != first_session_id
    assert next_session.operational_state == OperationalState.DISARMED.value
    assert next_session.session_net_pnl == Decimal("0")
    assert db_session.scalar(
        select(func.count()).select_from(RiskStateEvent).where(
            RiskStateEvent.session_id == first_session_id
        )
    ) >= 2


@pytest.mark.parametrize("purpose", ["TAKE_PROFIT", "STOP_LOSS"])
def test_hard_halt_supersedes_ordinary_exit_purposes(
    db_session: Session, purpose: str
) -> None:
    context = seed_context(db_session, position_quantity=Decimal("1"))
    governor = initialize_governor(db_session)
    governor.record_fill_accounting(
        accounting_event("-6"), authorized_cash_usd=Decimal("0")
    )
    result = governor.evaluate(
        make_request(db_session, context, purpose=purpose, side="SELL")
    )
    assert result.reason is DisqualificationReason.SYSTEM_HALTED
