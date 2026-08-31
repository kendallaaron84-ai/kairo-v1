import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.broker import BrokerAccount
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
from app.db.models.projections import CapitalCell
from app.db.models.risk import RiskSession
from engine.execution.lineage_gate import ExecutionAuthorizationError
from engine.execution.models import (
    ExecutionQuote,
    LiquidityFidelityTier,
    PaperEngineConfig,
)
from engine.execution.paper_broker import PaperExecutionEngine


pytestmark = pytest.mark.integration


@dataclass
class Context:
    broker: BrokerAccount
    instrument: Instrument
    intent: OrderIntent
    decision: RiskDecision
    order: KairoOrder
    snapshot: MarketSnapshot


def seed_context(
    session: Session,
    *,
    side: str = "BUY",
    quantity: Decimal = Decimal("1"),
    order_type: str = "MARKET",
    limit_price: Decimal | None = None,
    verdict: str = "AUTHORIZED",
    multiplier: Decimal = Decimal("100"),
    bid: Decimal = Decimal("9.90"),
    ask: Decimal = Decimal("10.00"),
    bid_size: Decimal = Decimal("10"),
    ask_size: Decimal = Decimal("10"),
) -> Context:
    broker = BrokerAccount(
        broker_account_id=uuid4(),
        account_key=f"paper-{uuid4()}",
        broker_name="PAPER_SIM_001",
        environment="PAPER",
        status="ACTIVE",
    )
    instrument = Instrument(
        instrument_id=uuid4(),
        symbol=f"OPT{uuid4().hex[:8]}",
        asset_class="OPTION",
        currency="USD",
        underlying_symbol="TQQQ",
        contract_symbol=f"TQQQ{uuid4().hex[:16]}",
        expiration_date=date(2026, 9, 4),
        strike_price=Decimal("50"),
        option_right="CALL",
        contract_multiplier=multiplier,
        listing_type="STANDARD",
    )
    strategy = StrategyRegistry(
        strategy_id=f"PAPER-{uuid4().hex[:8]}",
        version_tag="1.0.0",
        display_name="Paper execution test",
        status="ACTIVE",
        configuration={"clearance": "PAPER_ONLY"},
    )
    session.add_all([broker, instrument, strategy])
    session.flush()
    cell = CapitalCell(
        cell_id=uuid4(),
        cell_code=f"CELL-{uuid4().hex[:8]}",
        seed_capital=Decimal("1000"),
        status="ACTIVE",
        autonomy_tier="APPRENTICE",
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag,
        target_treasury_code="META",
    )
    session.add(cell)
    session.flush()
    intent = OrderIntent(
        intent_id=uuid4(),
        cell_id=cell.cell_id,
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag,
        instrument_id=instrument.instrument_id,
        client_order_key=f"paper-intent-{uuid4()}",
        order_purpose="ENTRY",
        side=side,
        target_quantity=quantity,
        order_type=order_type,
        limit_price=limit_price,
    )
    risk_session = RiskSession(
        session_id=f"paper-session-{uuid4()}",
        trading_date=date.today(),
        session_open=datetime.now(UTC) - timedelta(hours=1),
        session_close=datetime.now(UTC) + timedelta(hours=6),
    )
    session.add_all([intent, risk_session])
    session.flush()
    decision = RiskDecision(
        decision_id=uuid4(),
        intent_id=intent.intent_id,
        session_id=risk_session.session_id,
        verdict=verdict,
        reason_code="TEST_DECISION",
        operational_state="ARMED",
        intent_classification="EXPOSURE_INCREASING",
        session_net_pnl=Decimal("0"),
        authorized_cash_usd=Decimal("1000"),
        requested_cash_usd=Decimal("100"),
        projected_exposure_usd=Decimal("100"),
        max_contractual_loss_usd=Decimal("100"),
        details={},
    )
    session.add(decision)
    session.flush()
    order = KairoOrder(
        kairo_order_id=uuid4(),
        intent_id=intent.intent_id,
        risk_decision_id=decision.decision_id,
        broker_account_id=broker.broker_account_id,
        status="PENDING_SUBMIT",
    )
    snapshot = MarketSnapshot(
        snapshot_id=uuid4(),
        instrument_id=instrument.instrument_id,
        captured_at=datetime.now(UTC),
        bid=bid,
        ask=ask,
        last=(bid + ask) / Decimal("2"),
        payload={"bid_size": str(bid_size), "ask_size": str(ask_size)},
    )
    session.add_all([order, snapshot])
    session.flush()
    return Context(broker, instrument, intent, decision, order, snapshot)


def quote(
    context: Context,
    *,
    tier: LiquidityFidelityTier = LiquidityFidelityTier.TIER_1_QUOTE_DEPTH,
    bid_size: Decimal = Decimal("10"),
    ask_size: Decimal = Decimal("10"),
) -> ExecutionQuote:
    return ExecutionQuote(
        snapshot_id=context.snapshot.snapshot_id,
        instrument_id=context.instrument.instrument_id,
        bid=context.snapshot.bid,
        ask=context.snapshot.ask,
        bid_size=bid_size,
        ask_size=ask_size,
        captured_at=context.snapshot.captured_at,
        fidelity_tier=tier,
    )


def add_quote_snapshot(
    session: Session,
    context: Context,
    *,
    bid_size: Decimal,
    ask_size: Decimal,
) -> ExecutionQuote:
    snapshot = MarketSnapshot(
        snapshot_id=uuid4(),
        instrument_id=context.instrument.instrument_id,
        captured_at=datetime.now(UTC),
        bid=context.snapshot.bid,
        ask=context.snapshot.ask,
        last=context.snapshot.last,
        payload={"bid_size": str(bid_size), "ask_size": str(ask_size)},
    )
    session.add(snapshot)
    session.flush()
    return ExecutionQuote(
        snapshot_id=snapshot.snapshot_id,
        instrument_id=context.instrument.instrument_id,
        bid=snapshot.bid,
        ask=snapshot.ask,
        bid_size=bid_size,
        ask_size=ask_size,
        captured_at=snapshot.captured_at,
        fidelity_tier=LiquidityFidelityTier.TIER_1_QUOTE_DEPTH,
    )


def bar_quote(session: Session, context: Context) -> ExecutionQuote:
    values = {
        "bar_open": "9.80",
        "bar_high": "10.20",
        "bar_low": "9.70",
        "bar_close": "10.00",
        "bar_volume": "1000000",
    }
    snapshot = MarketSnapshot(
        snapshot_id=uuid4(),
        instrument_id=context.instrument.instrument_id,
        captured_at=datetime.now(UTC),
        last=Decimal(values["bar_close"]),
        payload=values,
    )
    session.add(snapshot)
    session.flush()
    return ExecutionQuote(
        snapshot_id=snapshot.snapshot_id,
        instrument_id=context.instrument.instrument_id,
        captured_at=snapshot.captured_at,
        fidelity_tier=LiquidityFidelityTier.TIER_3_BAR_ONLY,
        **{name: Decimal(value) for name, value in values.items()},
    )


def engine(
    session: Session,
    context: Context,
    *,
    slippage: Decimal = Decimal("0"),
) -> PaperExecutionEngine:
    return PaperExecutionEngine(
        session,
        PaperEngineConfig(
            broker_account_id=context.broker.broker_account_id,
            default_slippage_bps=slippage,
        ),
    )


def direct_simulated_fill_values(context: Context) -> dict:
    return {
        "fill_id": uuid4(),
        "kairo_order_id": context.order.kairo_order_id,
        "broker_account_id": context.broker.broker_account_id,
        "broker_fill_id": f"direct-{uuid4()}",
        "instrument_id": context.instrument.instrument_id,
        "side": context.intent.side,
        "quantity": Decimal("1"),
        "price": Decimal("10.01"),
        "reference_price": Decimal("10.00"),
        "contract_multiplier": context.instrument.contract_multiplier,
        "slippage_usd": Decimal("1.00"),
        "commission_fee_usd": Decimal("0"),
        "is_simulated": True,
        "liquidity_fidelity_tier": "TIER_1_QUOTE_DEPTH",
        "simulation_model": "PAPER-FILL-v0.1",
        "simulation_policy_version": "QUOTE-DEPTH-v0.1",
        "source_snapshot_id": context.snapshot.snapshot_id,
        "simulation_metadata": {
            "synthetic": True,
            "execution_guaranteed": False,
        },
    }


def run(coro):
    return asyncio.run(coro)


def test_unauthorized_kairo_order_cannot_reach_paper_execution(
    db_session: Session,
) -> None:
    context = seed_context(db_session, verdict="BLOCKED")
    with pytest.raises(ExecutionAuthorizationError, match="not AUTHORIZED"):
        run(engine(db_session, context).submit_order(context.order.kairo_order_id, quote(context)))
    assert db_session.scalar(select(func.count()).select_from(OrderObservation)) == 0
    assert db_session.scalar(select(func.count()).select_from(Fill)) == 0


def test_paper_engine_uses_canonical_broker_account_uuid(db_session: Session) -> None:
    context = seed_context(db_session)
    run(engine(db_session, context).submit_order(context.order.kairo_order_id, quote(context)))
    fills = list(db_session.scalars(select(Fill)))
    observations = list(db_session.scalars(select(OrderObservation)))
    assert fills and observations
    assert {item.broker_account_id for item in fills + observations} == {
        context.broker.broker_account_id
    }


def test_paper_buy_order_fills_at_ask_price(db_session: Session) -> None:
    context = seed_context(db_session, side="BUY")
    receipt = run(
        engine(db_session, context).submit_order(context.order.kairo_order_id, quote(context))
    )
    assert receipt.fill_records[0].reference_price == context.snapshot.ask
    assert receipt.fill_records[0].fill_price == context.snapshot.ask


def test_paper_sell_order_fills_at_bid_price(db_session: Session) -> None:
    context = seed_context(db_session, side="SELL")
    receipt = run(
        engine(db_session, context).submit_order(context.order.kairo_order_id, quote(context))
    )
    assert receipt.fill_records[0].reference_price == context.snapshot.bid
    assert receipt.fill_records[0].fill_price == context.snapshot.bid


def test_buy_limit_slippage_never_exceeds_limit_price(db_session: Session) -> None:
    context = seed_context(
        db_session, side="BUY", order_type="LIMIT", limit_price=Decimal("10.05")
    )
    receipt = run(
        engine(db_session, context, slippage=Decimal("0.10")).submit_order(
            context.order.kairo_order_id, quote(context)
        )
    )
    assert receipt.fill_records[0].fill_price == Decimal("10.05")


def test_sell_limit_slippage_never_falls_below_limit_price(db_session: Session) -> None:
    context = seed_context(
        db_session,
        side="SELL",
        order_type="LIMIT",
        limit_price=Decimal("9.85"),
    )
    receipt = run(
        engine(db_session, context, slippage=Decimal("0.10")).submit_order(
            context.order.kairo_order_id, quote(context)
        )
    )
    assert receipt.fill_records[0].fill_price == Decimal("9.85")


def test_unknown_depth_does_not_fabricate_partial_fill_capacity(
    db_session: Session,
) -> None:
    context = seed_context(db_session, quantity=Decimal("3"))
    receipt = run(
        engine(db_session, context).submit_order(
            context.order.kairo_order_id, bar_quote(db_session, context)
        )
    )
    fill = receipt.fill_records[0]
    assert fill.quantity == Decimal("3")
    assert fill.liquidity_fidelity_tier is LiquidityFidelityTier.TIER_3_BAR_ONLY
    assert fill.simulation_policy_version == "BAR-COARSE-CONSERVATIVE-v0.1"
    assert fill.simulation_metadata["execution_guaranteed"] is False
    assert fill.simulation_metadata["bar_volume_used_as_depth"] is False
    assert fill.simulation_metadata["partial_fill_capacity_inferred"] is False


def test_partial_fill_creates_distinct_fill_rows(db_session: Session) -> None:
    context = seed_context(db_session, quantity=Decimal("1"), ask_size=Decimal("0.4"))
    first_quote = quote(context, ask_size=Decimal("0.4"))
    second_quote = add_quote_snapshot(
        db_session, context, bid_size=Decimal("10"), ask_size=Decimal("0.6")
    )
    paper = engine(db_session, context)
    run(paper.submit_order(context.order.kairo_order_id, first_quote))
    run(paper.process_quote(context.order.kairo_order_id, second_quote))
    fills = list(db_session.scalars(select(Fill).order_by(Fill.filled_at)))
    assert [item.quantity for item in fills] == [Decimal("0.4"), Decimal("0.6")]
    assert len({item.fill_id for item in fills}) == 2


def test_cumulative_fill_quantity_matches_fill_facts(db_session: Session) -> None:
    context = seed_context(db_session, quantity=Decimal("1"), ask_size=Decimal("0.4"))
    paper = engine(db_session, context)
    run(
        paper.submit_order(
            context.order.kairo_order_id,
            quote(context, ask_size=Decimal("0.4")),
        )
    )
    receipt = run(
        paper.process_quote(
            context.order.kairo_order_id,
            add_quote_snapshot(
                db_session, context, bid_size=Decimal("10"), ask_size=Decimal("0.6")
            ),
        )
    )
    persisted = db_session.scalar(
        select(func.sum(Fill.quantity)).where(
            Fill.kairo_order_id == context.order.kairo_order_id
        )
    )
    assert receipt.cumulative_filled_qty == persisted == Decimal("1")


def test_cancel_request_does_not_claim_canceled_before_transition(
    db_session: Session,
) -> None:
    context = seed_context(db_session, quantity=Decimal("2"), ask_size=Decimal("0.5"))
    paper = engine(db_session, context)
    run(
        paper.submit_order(
            context.order.kairo_order_id,
            quote(context, ask_size=Decimal("0.5")),
        )
    )
    result = run(paper.request_cancel(context.order.kairo_order_id))
    statuses = list(
        db_session.scalars(
            select(OrderObservation.status).where(
                OrderObservation.kairo_order_id == context.order.kairo_order_id
            )
        )
    )
    assert result["status"] == "CANCEL_REQUESTED"
    assert statuses[-1] == "CANCEL_REQUESTED"
    assert "CANCELED" not in statuses


def test_paper_fill_provenance_is_explicitly_synthetic(db_session: Session) -> None:
    context = seed_context(db_session)
    run(engine(db_session, context).submit_order(context.order.kairo_order_id, quote(context)))
    fill = db_session.scalar(select(Fill))
    observations = list(db_session.scalars(select(OrderObservation)))
    assert fill is not None and fill.is_simulated is True
    assert fill.simulation_model == "PAPER-FILL-v0.1"
    assert fill.liquidity_fidelity_tier == "TIER_1_QUOTE_DEPTH"
    assert all(item.payload["source"] == "PAPER_ENGINE" for item in observations)
    assert all(item.payload["synthetic"] is True for item in observations)
    assert all("simulation_policy_version" in item.payload for item in observations)


def test_paper_fill_cannot_create_reconciliation_or_settlement_evidence(
    db_session: Session,
) -> None:
    context = seed_context(db_session)
    before = (
        db_session.scalar(select(func.count()).select_from(BrokerCashSnapshot)),
        db_session.scalar(
            select(func.count()).select_from(KairoCapitalAuthorizationRecord)
        ),
    )
    run(engine(db_session, context).submit_order(context.order.kairo_order_id, quote(context)))
    after = (
        db_session.scalar(select(func.count()).select_from(BrokerCashSnapshot)),
        db_session.scalar(
            select(func.count()).select_from(KairoCapitalAuthorizationRecord)
        ),
    )
    assert after == before


def test_adverse_slippage_scales_with_canonical_contract_multiplier(
    db_session: Session,
) -> None:
    context = seed_context(
        db_session, quantity=Decimal("2"), multiplier=Decimal("10")
    )
    receipt = run(
        engine(db_session, context, slippage=Decimal("0.01")).submit_order(
            context.order.kairo_order_id, quote(context)
        )
    )
    fill = receipt.fill_records[0]
    assert fill.contract_multiplier == Decimal("10")
    assert fill.slippage_usd == Decimal("2.0000")


def test_duplicate_fill_idempotency_prevents_double_persistence(
    db_session: Session,
) -> None:
    context = seed_context(db_session)
    paper = engine(db_session, context)
    evidence = quote(context)
    first = run(paper.submit_order(context.order.kairo_order_id, evidence))
    second = run(paper.submit_order(context.order.kairo_order_id, evidence))
    count = db_session.scalar(
        select(func.count()).select_from(Fill).where(
            Fill.kairo_order_id == context.order.kairo_order_id
        )
    )
    assert first.cumulative_filled_qty == second.cumulative_filled_qty == Decimal("1")
    assert count == 1


def test_direct_simulated_fill_without_source_snapshot_is_rejected(
    db_session: Session,
) -> None:
    context = seed_context(db_session)
    values = direct_simulated_fill_values(context)
    values["source_snapshot_id"] = None
    with pytest.raises(IntegrityError):
        db_session.execute(insert(Fill).values(**values))


def test_direct_simulated_fill_without_synthetic_true_is_rejected(
    db_session: Session,
) -> None:
    context = seed_context(db_session)
    values = direct_simulated_fill_values(context)
    values["simulation_metadata"] = {"execution_guaranteed": False}
    with pytest.raises(IntegrityError):
        db_session.execute(insert(Fill).values(**values))


def test_direct_simulated_fill_without_execution_guaranteed_is_rejected(
    db_session: Session,
) -> None:
    context = seed_context(db_session)
    values = direct_simulated_fill_values(context)
    values["simulation_metadata"] = {"synthetic": True}
    with pytest.raises(IntegrityError):
        db_session.execute(insert(Fill).values(**values))


def test_direct_complete_simulated_fill_succeeds(db_session: Session) -> None:
    context = seed_context(db_session)
    values = direct_simulated_fill_values(context)
    db_session.execute(insert(Fill).values(**values))
    persisted = db_session.get(Fill, values["fill_id"])
    assert persisted is not None
    assert persisted.source_snapshot_id == context.snapshot.snapshot_id
    assert persisted.simulation_metadata["synthetic"] is True
    assert persisted.simulation_metadata["execution_guaranteed"] is False
