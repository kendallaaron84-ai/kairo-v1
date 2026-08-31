from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.broker import BrokerAccount
from app.db.models.configuration import Instrument, StrategyRegistry
from app.db.models.ledger import (
    Fill,
    KairoOrder,
    MarketSnapshot,
    OrderIntent,
    OrderObservation,
    SiphonEvent,
)


pytestmark = pytest.mark.integration


def seed_configuration(session: Session) -> tuple[BrokerAccount, Instrument, StrategyRegistry]:
    broker = BrokerAccount(
        broker_account_id=uuid4(), account_key=f"paper-{uuid4()}", broker_name="TEST",
        environment="PAPER", status="ACTIVE",
    )
    instrument = Instrument(
        instrument_id=uuid4(), symbol=f"T{uuid4().hex[:7]}", asset_class="EQUITY", currency="USD"
    )
    strategy = StrategyRegistry(
        strategy_id=f"EMA-{uuid4().hex[:8]}", version_tag="1.0.0",
        display_name="Test strategy", status="ACTIVE", configuration={},
    )
    session.add_all([broker, instrument, strategy])
    session.flush()
    return broker, instrument, strategy


def make_intent(
    *, session: Session, instrument: Instrument, strategy: StrategyRegistry,
    client_order_key: str | None = None, siphon_id=None,
) -> OrderIntent:
    intent = OrderIntent(
        intent_id=uuid4(), cell_id=uuid4(), strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag, instrument_id=instrument.instrument_id,
        siphon_id=siphon_id, client_order_key=client_order_key or f"intent-{uuid4()}",
        order_purpose="ENTRY", side="BUY", target_quantity=Decimal("1"),
        order_type="MARKET",
    )
    session.add(intent)
    session.flush()
    return intent


def test_strategy_to_fill_provenance_is_enforceable(db_session: Session) -> None:
    broker, instrument, strategy = seed_configuration(db_session)
    intent = make_intent(session=db_session, instrument=instrument, strategy=strategy)
    order = KairoOrder(
        kairo_order_id=uuid4(), intent_id=intent.intent_id,
        broker_account_id=broker.broker_account_id, status="SUBMITTED",
    )
    db_session.add(order)
    db_session.flush()
    observation = OrderObservation(
        observation_id=uuid4(), kairo_order_id=order.kairo_order_id,
        broker_account_id=broker.broker_account_id, broker_observation_key=f"obs-{uuid4()}",
        broker_order_id="broker-order-1", event_type="STATUS", status="FILLED", payload={},
    )
    fill = Fill(
        fill_id=uuid4(), kairo_order_id=order.kairo_order_id,
        broker_account_id=broker.broker_account_id, broker_fill_id="fill-1",
        instrument_id=instrument.instrument_id, side="BUY",
        quantity=Decimal("1"), price=Decimal("78.25"),
    )
    db_session.add_all([observation, fill])
    db_session.flush()
    assert fill.kairo_order_id == observation.kairo_order_id == order.kairo_order_id
    assert order.intent_id == intent.intent_id
    assert (intent.strategy_id, intent.strategy_version) == (
        strategy.strategy_id, strategy.version_tag,
    )


@pytest.mark.parametrize("bad_field", ["strategy", "instrument", "siphon"])
def test_invalid_intent_lineage_is_rejected(db_session: Session, bad_field: str) -> None:
    _, instrument, strategy = seed_configuration(db_session)
    db_session.add(
        OrderIntent(
            intent_id=uuid4(), cell_id=uuid4(),
            strategy_id="MISSING" if bad_field == "strategy" else strategy.strategy_id,
            strategy_version="9.9.9" if bad_field == "strategy" else strategy.version_tag,
            instrument_id=uuid4() if bad_field == "instrument" else instrument.instrument_id,
            siphon_id=uuid4() if bad_field == "siphon" else None,
            client_order_key=f"bad-{uuid4()}", order_purpose="ENTRY", side="BUY",
            target_quantity=Decimal("1"), order_type="MARKET",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_siphon_to_treasury_order_lineage(db_session: Session) -> None:
    _, instrument, strategy = seed_configuration(db_session)
    siphon = SiphonEvent(
        siphon_id=uuid4(), cell_id=uuid4(), treasury_code="META",
        amount=Decimal("2.14"), reason_code="PROFIT_SIPHON",
        policy_id="LEGACY-SIPHON-v0", policy_version="0.0.0",
        source_fill_ids=[], qualified_profit_usd=Decimal("2.14"),
        safety_reserve_usd=Decimal("0"), target_treasury_usd=Decimal("0"),
        replication_pool_usd=Decimal("2.14"), is_synthetic=False,
    )
    db_session.add(siphon)
    db_session.flush()
    intent = make_intent(
        session=db_session, instrument=instrument, strategy=strategy, siphon_id=siphon.siphon_id
    )
    assert intent.siphon_id == siphon.siphon_id


def test_market_snapshot_requires_canonical_instrument(db_session: Session) -> None:
    db_session.add(
        MarketSnapshot(snapshot_id=uuid4(), instrument_id=uuid4(), payload={})
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_duplicate_client_order_key_is_rejected(db_session: Session) -> None:
    _, instrument, strategy = seed_configuration(db_session)
    key = f"duplicate-{uuid4()}"
    make_intent(session=db_session, instrument=instrument, strategy=strategy, client_order_key=key)
    db_session.add(
        OrderIntent(
            intent_id=uuid4(), cell_id=uuid4(), strategy_id=strategy.strategy_id,
            strategy_version=strategy.version_tag, instrument_id=instrument.instrument_id,
            client_order_key=key, order_purpose="ENTRY", side="BUY",
            target_quantity=Decimal("1"), order_type="MARKET",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_orphan_broker_order_is_rejected(db_session: Session) -> None:
    broker, _, _ = seed_configuration(db_session)
    db_session.add(
        KairoOrder(
            kairo_order_id=uuid4(), intent_id=uuid4(),
            broker_account_id=broker.broker_account_id, status="SUBMITTED",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_duplicate_broker_fill_is_rejected(db_session: Session) -> None:
    broker, instrument, strategy = seed_configuration(db_session)
    intent = make_intent(session=db_session, instrument=instrument, strategy=strategy)
    order = KairoOrder(
        kairo_order_id=uuid4(), intent_id=intent.intent_id,
        broker_account_id=broker.broker_account_id, status="SUBMITTED",
    )
    db_session.add(order)
    db_session.flush()
    for _ in range(2):
        db_session.add(
            Fill(
                fill_id=uuid4(), kairo_order_id=order.kairo_order_id,
                broker_account_id=broker.broker_account_id, broker_fill_id="same-fill",
                instrument_id=instrument.instrument_id, side="BUY",
                quantity=Decimal("1"), price=Decimal("78.25"),
            )
        )
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_duplicate_order_observation_is_rejected(db_session: Session) -> None:
    broker, instrument, strategy = seed_configuration(db_session)
    intent = make_intent(session=db_session, instrument=instrument, strategy=strategy)
    order = KairoOrder(
        kairo_order_id=uuid4(), intent_id=intent.intent_id,
        broker_account_id=broker.broker_account_id, status="SUBMITTED",
    )
    db_session.add(order)
    db_session.flush()
    for _ in range(2):
        db_session.add(
            OrderObservation(
                observation_id=uuid4(), kairo_order_id=order.kairo_order_id,
                broker_account_id=broker.broker_account_id,
                broker_observation_key="same-message", broker_order_id="order-1",
                event_type="STATUS", status="OPEN", payload={},
            )
        )
    with pytest.raises(IntegrityError):
        db_session.flush()
