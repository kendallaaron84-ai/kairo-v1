import gc
import weakref
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.ledger import Fill, OrderIntent
from app.db.models.projections import CurrentPosition
from app.db.models.risk import RiskInstrumentMark, RiskSession, RiskStateEvent
from engine.execution.replay_orchestrator import ReplayOrchestrator
from engine.risk.governor import RiskGovernor
from engine.risk.models import (
    DecisionVerdict,
    DisqualificationReason,
    FillAccountingEvent,
    MarketMark,
)


pytestmark = pytest.mark.integration


def accounting_event(
    verification_replay,
    realized: str,
    *,
    fees: str = "0",
    slippage: str = "0",
) -> FillAccountingEvent:
    return FillAccountingEvent(
        fill_id=uuid4(),
        kairo_order_id=uuid4(),
        broker_account_id=verification_replay.broker.broker_account_id,
        instrument_id=verification_replay.call.instrument_id,
        realized_pnl_delta_usd=Decimal(realized),
        commission_fees_usd=Decimal(fees),
        slippage_usd=Decimal(slippage),
        fill_price=Decimal("0.50"),
        filled_qty=Decimal("1"),
        timestamp=verification_replay.session_open(),
    )


def test_m1_ten_session_marathon_executes_with_clean_connection_pool(
    db_session: Session, verification_replay
) -> None:
    engine = db_session.get_bind().engine
    baseline_checkouts = engine.pool.checkedout()
    references: list[weakref.ReferenceType] = []
    for day in range(10):
        nested = db_session.begin_nested()
        orchestrator = ReplayOrchestrator(
            db_session,
            verification_replay.config(
                session_id=f"M1-MARATHON-{day}", day=day, authorized=True
            ),
        )
        result = orchestrator.replay_legacy(
            (verification_replay.legacy_stream(day=day, with_chains=False),)
        )
        assert result.event_count == 11
        assert orchestrator.governor.current_state().operational_state == "ARMED"
        nested.commit()
        assert nested.is_active is False
        references.append(weakref.ref(orchestrator))
        del orchestrator
    gc.collect()
    assert all(reference() is None for reference in references)
    assert engine.pool.checkedout() == baseline_checkouts
    assert db_session.is_active is True
    assert db_session.scalar(select(func.count()).select_from(RiskSession)) == 10
    assert db_session.scalar(select(func.count()).select_from(OrderIntent)) == 0


def test_m1_current_position_matches_sum_of_signed_fills(
    db_session: Session, verification_replay
) -> None:
    orchestrator, _ = verification_replay.run_entry()
    verification_replay.close_position(orchestrator)
    fills = list(db_session.scalars(select(Fill).order_by(Fill.filled_at, Fill.fill_id)))
    signed_quantity = sum(
        (
            fill.quantity if fill.side == "BUY" else -fill.quantity
            for fill in fills
        ),
        Decimal("0"),
    )
    projection = db_session.scalar(
        select(CurrentPosition).where(
            CurrentPosition.cell_id == verification_replay.cell.cell_id,
            CurrentPosition.broker_account_id
            == verification_replay.broker.broker_account_id,
            CurrentPosition.instrument_id == verification_replay.call.instrument_id,
        )
    )
    assert signed_quantity == Decimal("0")
    assert projection is None or projection.quantity == signed_quantity
    assert len(fills) == 2
    buy = next(fill for fill in fills if fill.side == "BUY")
    sell = next(fill for fill in fills if fill.side == "SELL")
    realized_after_fees = (
        (sell.price - buy.price) * sell.quantity * sell.contract_multiplier
        - sum((fill.commission_fee_usd for fill in fills), Decimal("0"))
    )
    state = orchestrator.governor.current_state()
    assert state.session_net_pnl == realized_after_fees
    assert state.session_slippage_usd == sum(
        (fill.slippage_usd for fill in fills), Decimal("0")
    )


def test_m1_same_session_restart_preserves_halt_and_loss_state(
    db_session: Session, verification_replay
) -> None:
    governor = verification_replay.initialize_governor(session_id="M1-RESTART")
    governor.record_market_mark(
        MarketMark(
            instrument_id=verification_replay.call.instrument_id,
            mark_price=Decimal("0.49"),
            source_timestamp=verification_replay.session_open(),
            received_at=verification_replay.session_open(),
        ),
        positions=[],
        authorized_cash_usd=Decimal("1000"),
    )
    governor.record_fill_accounting(
        accounting_event(
            verification_replay, "-5", fees="1", slippage="0.25"
        ),
        authorized_cash_usd=Decimal("1000"),
    )
    before = governor.current_state()
    event_ids = tuple(
        db_session.scalars(
            select(RiskStateEvent.event_id)
            .where(RiskStateEvent.session_id == "M1-RESTART")
            .order_by(RiskStateEvent.recorded_at, RiskStateEvent.event_id)
        )
    )

    restarted = ReplayOrchestrator(
        db_session,
        verification_replay.config(
            session_id="M1-RESTART", authorized=True
        ),
    )
    restarted.initialize()
    after = restarted.governor.current_state()
    assert after.operational_state == before.operational_state == "HALTED_HARD"
    assert after.session_realized_pnl == before.session_realized_pnl == Decimal("-5")
    assert after.session_fees_usd == before.session_fees_usd == Decimal("1")
    assert after.session_slippage_usd == Decimal("0.25")
    assert after.session_net_pnl == Decimal("-6")
    assert db_session.get(
        RiskInstrumentMark,
        ("M1-RESTART", verification_replay.call.instrument_id),
    ) is not None
    assert tuple(
        db_session.scalars(
            select(RiskStateEvent.event_id)
            .where(RiskStateEvent.session_id == "M1-RESTART")
            .order_by(RiskStateEvent.recorded_at, RiskStateEvent.event_id)
        )
    ) == event_ids


def test_m1_new_session_rollover_resets_pnl_and_requires_rearm(
    db_session: Session, verification_replay
) -> None:
    first = verification_replay.initialize_governor(session_id="M1-OLD-SESSION")
    first.record_fill_accounting(
        accounting_event(verification_replay, "20"),
        authorized_cash_usd=Decimal("1000"),
    )
    assert first.current_state().operational_state == "LOCKED_FOR_DAY"
    old_event_ids = tuple(
        db_session.scalars(
            select(RiskStateEvent.event_id).where(
                RiskStateEvent.session_id == "M1-OLD-SESSION"
            )
        )
    )

    runtime = ReplayOrchestrator(
        db_session,
        verification_replay.config(
            session_id="M1-NEW-SESSION", day=1, authorized=False
        ),
    )
    runtime.initialize()
    state = runtime.governor.current_state()
    assert state.operational_state == "DISARMED"
    assert state.session_realized_pnl == Decimal("0")
    assert state.session_unrealized_pnl == Decimal("0")
    assert state.session_fees_usd == Decimal("0")
    assert state.session_net_pnl == Decimal("0")
    rejected = runtime.governor.evaluate(verification_replay.risk_request())
    assert rejected.verdict is DecisionVerdict.REJECTED
    assert rejected.reason is DisqualificationReason.NOT_ARMED
    runtime.governor.arm(authorized_cash_usd=Decimal("1000"))
    authorized = runtime.governor.evaluate(verification_replay.risk_request())
    assert authorized.verdict is DecisionVerdict.AUTHORIZED
    assert tuple(
        db_session.scalars(
            select(RiskStateEvent.event_id).where(
                RiskStateEvent.session_id == "M1-OLD-SESSION"
            )
        )
    ) == old_event_ids


def test_m1_minus_six_halt_blocks_entry_but_allows_exit(
    db_session: Session, verification_replay
) -> None:
    governor = verification_replay.initialize_governor(session_id="M1-LOSS-GATE")
    governor.record_fill_accounting(
        accounting_event(verification_replay, "-6"),
        authorized_cash_usd=Decimal("1000"),
    )
    entry = governor.evaluate(verification_replay.risk_request())
    assert entry.verdict is DecisionVerdict.REJECTED
    position = _position(db_session, verification_replay)
    exit_result = governor.evaluate(
        verification_replay.risk_request(
            purpose="EMERGENCY_EXIT", side="SELL", position=position
        )
    )
    assert exit_result.verdict is DecisionVerdict.AUTHORIZED


def test_m1_plus_twenty_lock_blocks_entry_but_allows_exit(
    db_session: Session, verification_replay
) -> None:
    governor = verification_replay.initialize_governor(session_id="M1-PROFIT-GATE")
    governor.record_fill_accounting(
        accounting_event(verification_replay, "20"),
        authorized_cash_usd=Decimal("1000"),
    )
    entry = governor.evaluate(verification_replay.risk_request())
    assert entry.verdict is DecisionVerdict.REJECTED
    assert entry.reason is DisqualificationReason.PROFIT_CEILING_REACHED
    position = _position(db_session, verification_replay)
    exit_result = governor.evaluate(
        verification_replay.risk_request(
            purpose="TAKE_PROFIT", side="SELL", position=position
        )
    )
    assert exit_result.verdict is DecisionVerdict.AUTHORIZED


def _position(db_session: Session, verification_replay) -> CurrentPosition:
    position = CurrentPosition(
        position_id=uuid4(),
        cell_id=verification_replay.cell.cell_id,
        broker_account_id=verification_replay.broker.broker_account_id,
        instrument_id=verification_replay.call.instrument_id,
        quantity=Decimal("1"),
        average_price=Decimal("0.50"),
        updated_at=verification_replay.session_open() + timedelta(hours=1),
    )
    db_session.add(position)
    db_session.flush()
    return position
