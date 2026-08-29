from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.broker import BrokerAccount, BrokerInstrumentCapability
from app.db.models.configuration import Instrument, StrategyRegistry, TrustPolicy
from app.db.models.ledger import (
    BrokerCashSnapshot,
    KairoCapitalAuthorizationRecord,
    OrderIntent,
    TrustEvaluation,
)
from app.db.models.projections import CapitalCell, CurrentPosition


pytestmark = pytest.mark.integration


def add_broker(session: Session) -> BrokerAccount:
    broker = BrokerAccount(
        broker_account_id=uuid4(),
        account_key=f"paper-{uuid4()}",
        broker_name="TEST",
        environment="PAPER",
        status="ACTIVE",
    )
    session.add(broker)
    session.flush()
    return broker


def add_equity(session: Session) -> Instrument:
    instrument = Instrument(
        instrument_id=uuid4(),
        symbol=f"E{uuid4().hex[:7]}",
        asset_class="EQUITY",
        currency="USD",
    )
    session.add(instrument)
    session.flush()
    return instrument


def add_strategy(session: Session) -> StrategyRegistry:
    strategy = StrategyRegistry(
        strategy_id=f"STRAT-{uuid4().hex[:8]}",
        version_tag="1.0.0",
        display_name="Conformance strategy",
        status="ACTIVE",
        configuration={},
    )
    session.add(strategy)
    session.flush()
    return strategy


def add_cell(session: Session) -> CapitalCell:
    strategy = add_strategy(session)
    cell = CapitalCell(
        cell_id=uuid4(),
        cell_code=f"CELL-{uuid4().hex[:8]}",
        seed_capital=Decimal("100"),
        status="APPRENTICE",
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag,
        target_treasury_code="META",
    )
    session.add(cell)
    session.flush()
    return cell


def test_complete_option_identity_is_first_class(db_session: Session) -> None:
    option = Instrument(
        instrument_id=uuid4(),
        symbol=f"OPT{uuid4().hex[:8]}",
        asset_class="OPTION",
        currency="USD",
        underlying_symbol="TQQQ",
        contract_symbol=f"TQQQ{uuid4().hex[:16]}",
        expiration_date=date(2026, 8, 28),
        strike_price=Decimal("75"),
        option_right="CALL",
        contract_multiplier=Decimal("100"),
        listing_type="STANDARD",
    )
    db_session.add(option)
    db_session.flush()
    assert option.underlying_symbol == "TQQQ"
    assert option.strike_price == Decimal("75")
    assert option.option_right == "CALL"


@pytest.mark.parametrize(
    "missing_field",
    [
        "underlying_symbol",
        "contract_symbol",
        "expiration_date",
        "strike_price",
        "option_right",
        "contract_multiplier",
        "listing_type",
    ],
)
def test_incomplete_option_identity_is_rejected(
    db_session: Session, missing_field: str
) -> None:
    identity = {
        "underlying_symbol": "TQQQ",
        "contract_symbol": f"TQQQ{uuid4().hex[:16]}",
        "expiration_date": date(2026, 8, 28),
        "strike_price": Decimal("75"),
        "option_right": "CALL",
        "contract_multiplier": Decimal("100"),
        "listing_type": "STANDARD",
    }
    identity[missing_field] = None
    db_session.add(
        Instrument(
            instrument_id=uuid4(),
            symbol=f"OPT{uuid4().hex[:8]}",
            asset_class="OPTION",
            currency="USD",
            **identity,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_broker_capability_restores_execution_support_flags(
    db_session: Session,
) -> None:
    broker = add_broker(db_session)
    instrument = add_equity(db_session)
    capability = BrokerInstrumentCapability(
        capability_id=uuid4(),
        broker_account_id=broker.broker_account_id,
        instrument_id=instrument.instrument_id,
        can_trade=True,
        can_fractional=True,
        can_short=False,
        notional_orders_supported=True,
        options_supported=False,
        extended_hours_supported=True,
    )
    db_session.add(capability)
    db_session.flush()
    assert capability.notional_orders_supported is True
    assert capability.options_supported is False
    assert capability.extended_hours_supported is True


def test_notional_treasury_intent_and_quantity_stop_intent_are_canonical(
    db_session: Session,
) -> None:
    instrument = add_equity(db_session)
    strategy = add_strategy(db_session)
    notional = OrderIntent(
        intent_id=uuid4(),
        cell_id=uuid4(),
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag,
        instrument_id=instrument.instrument_id,
        client_order_key=f"notional-{uuid4()}",
        order_purpose="TREASURY_PURCHASE",
        side="BUY",
        target_notional_usd=Decimal("1.33"),
        order_type="MARKET",
    )
    stop = OrderIntent(
        intent_id=uuid4(),
        cell_id=uuid4(),
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag,
        instrument_id=instrument.instrument_id,
        client_order_key=f"stop-{uuid4()}",
        order_purpose="STOP_LOSS",
        side="SELL",
        target_quantity=Decimal("1"),
        order_type="STOP",
        stop_price=Decimal("72.50"),
    )
    db_session.add_all([notional, stop])
    db_session.flush()
    assert notional.target_quantity is None
    assert stop.stop_price == Decimal("72.50")


@pytest.mark.parametrize(
    ("notional", "quantity", "order_type", "limit_price", "stop_price"),
    [
        (None, None, "MARKET", None, None),
        (Decimal("10"), Decimal("1"), "MARKET", None, None),
        (None, Decimal("1"), "LIMIT", None, None),
        (None, Decimal("1"), "STOP", None, None),
    ],
)
def test_invalid_intent_sizing_or_price_semantics_are_rejected(
    db_session: Session,
    notional: Decimal | None,
    quantity: Decimal | None,
    order_type: str,
    limit_price: Decimal | None,
    stop_price: Decimal | None,
) -> None:
    instrument = add_equity(db_session)
    strategy = add_strategy(db_session)
    db_session.add(
        OrderIntent(
            intent_id=uuid4(),
            cell_id=uuid4(),
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.version_tag,
            instrument_id=instrument.instrument_id,
            client_order_key=f"invalid-{uuid4()}",
            order_purpose="ENTRY",
            side="BUY",
            target_notional_usd=notional,
            target_quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_unknown_order_purpose_is_rejected(db_session: Session) -> None:
    instrument = add_equity(db_session)
    strategy = add_strategy(db_session)
    db_session.add(
        OrderIntent(
            intent_id=uuid4(),
            cell_id=uuid4(),
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.version_tag,
            instrument_id=instrument.instrument_id,
            client_order_key=f"invalid-purpose-{uuid4()}",
            order_purpose="UNSPECIFIED",
            side="BUY",
            target_quantity=Decimal("1"),
            order_type="MARKET",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_cash_snapshot_preserves_settlement_distinctions_and_authorization_lineage(
    db_session: Session,
) -> None:
    broker = add_broker(db_session)
    snapshot = BrokerCashSnapshot(
        snapshot_id=uuid4(),
        broker_account_id=broker.broker_account_id,
        broker_cash=Decimal("125.75"),
        settled_cash=Decimal("100.00"),
        unsettled_cash=Decimal("25.75"),
        buying_power=Decimal("200.00"),
        currency="USD",
    )
    db_session.add(snapshot)
    db_session.flush()
    authorization = KairoCapitalAuthorizationRecord(
        authorization_id=uuid4(),
        cell_id=uuid4(),
        broker_snapshot_id=snapshot.snapshot_id,
        broker_account_id=broker.broker_account_id,
        settled_cash=Decimal("100.00"),
        safety_reserve=Decimal("10.00"),
        ownership_treasury_reserved=Decimal("5.00"),
        replication_reserve=Decimal("5.00"),
        committed_obligations=Decimal("0"),
        authorized_trading_cash=Decimal("80.00"),
    )
    db_session.add(authorization)
    db_session.flush()
    assert snapshot.broker_cash == Decimal("125.75")
    assert snapshot.unsettled_cash == Decimal("25.75")
    assert authorization.broker_snapshot_id == snapshot.snapshot_id


def test_capital_authorization_rejects_snapshot_account_mismatch(
    db_session: Session,
) -> None:
    snapshot_broker = add_broker(db_session)
    other_broker = add_broker(db_session)
    snapshot = BrokerCashSnapshot(
        snapshot_id=uuid4(),
        broker_account_id=snapshot_broker.broker_account_id,
        broker_cash=Decimal("100"),
        settled_cash=Decimal("100"),
        unsettled_cash=Decimal("0"),
        buying_power=Decimal("100"),
        currency="USD",
    )
    db_session.add(snapshot)
    db_session.flush()
    db_session.add(
        KairoCapitalAuthorizationRecord(
            authorization_id=uuid4(),
            cell_id=uuid4(),
            broker_snapshot_id=snapshot.snapshot_id,
            broker_account_id=other_broker.broker_account_id,
            settled_cash=Decimal("100"),
            safety_reserve=Decimal("0"),
            ownership_treasury_reserved=Decimal("0"),
            replication_reserve=Decimal("0"),
            committed_obligations=Decimal("0"),
            authorized_trading_cash=Decimal("100"),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_zero_evidence_trust_evaluation_persists_explicit_metadata(
    db_session: Session,
) -> None:
    cell = add_cell(db_session)
    policy = TrustPolicy(
        policy_id=uuid4(),
        version_tag="1.0.0",
        name="Trust v0.1",
        policy_document={},
    )
    db_session.add(policy)
    db_session.flush()
    evaluation = TrustEvaluation(
        evaluation_id=uuid4(),
        cell_id=cell.cell_id,
        policy_id=policy.policy_id,
        policy_version=policy.version_tag,
        score=None,
        outcome="PASS",
        eligible_for_promotion=False,
        evidence_trade_count=0,
        disqualifiers=[],
        factor_breakdown={},
        details={"session": "no-trade"},
    )
    db_session.add(evaluation)
    db_session.flush()
    assert evaluation.score is None
    assert evaluation.evidence_trade_count == 0
    assert evaluation.eligible_for_promotion is False


def test_zero_evidence_score_is_rejected(db_session: Session) -> None:
    cell = add_cell(db_session)
    policy = TrustPolicy(
        policy_id=uuid4(),
        version_tag="1.0.0",
        name="Trust v0.1",
        policy_document={},
    )
    db_session.add(policy)
    db_session.flush()
    db_session.add(
        TrustEvaluation(
            evaluation_id=uuid4(),
            cell_id=cell.cell_id,
            policy_id=policy.policy_id,
            policy_version=policy.version_tag,
            score=Decimal("50"),
            outcome="PASS",
            eligible_for_promotion=False,
            evidence_trade_count=0,
            disqualifiers=[],
            factor_breakdown={},
            details={},
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_current_position_requires_existing_capital_cell(db_session: Session) -> None:
    broker = add_broker(db_session)
    instrument = add_equity(db_session)
    db_session.add(
        CurrentPosition(
            position_id=uuid4(),
            cell_id=uuid4(),
            broker_account_id=broker.broker_account_id,
            instrument_id=instrument.instrument_id,
            quantity=Decimal("1"),
            average_price=Decimal("75"),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_current_position_accepts_existing_capital_cell(db_session: Session) -> None:
    broker = add_broker(db_session)
    instrument = add_equity(db_session)
    strategy = add_strategy(db_session)
    cell = CapitalCell(
        cell_id=uuid4(),
        cell_code=f"CELL-{uuid4().hex[:8]}",
        seed_capital=Decimal("100"),
        status="APPRENTICE",
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag,
        target_treasury_code="META",
    )
    db_session.add(cell)
    db_session.flush()
    position = CurrentPosition(
        position_id=uuid4(),
        cell_id=cell.cell_id,
        broker_account_id=broker.broker_account_id,
        instrument_id=instrument.instrument_id,
        quantity=Decimal("1"),
        average_price=Decimal("75"),
    )
    db_session.add(position)
    db_session.flush()
    assert position.cell_id == cell.cell_id
