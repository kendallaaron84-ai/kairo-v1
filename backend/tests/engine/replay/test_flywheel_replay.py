from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models.broker import BrokerAccount
from app.db.models.configuration import CellTreasuryConfig, Instrument, StrategyRegistry
from app.db.models.ledger import (
    Fill,
    FillRealizedPnL,
    KairoCapitalAuthorizationRecord,
    KairoOrder,
    MarketSnapshot,
    OrderIntent,
    SiphonAllocation,
    SiphonEvent,
    SyntheticEvidenceManifest,
    TreasuryCashConsumption,
    TreasuryExecution,
)
from app.db.models.projections import CapitalCell, CurrentPosition, OwnershipTreasuryHolding
from app.db.models.replication import (
    CellGenesisEvent,
    CellReplicationProposal,
    ReplicationCashConsumption,
    ReplicationProposalReservation,
)
from app.db.models.risk import RiskGovernorState, RiskStateEvent
from engine.execution.virtual_clock import ReplayIdentityFactory, VirtualClock
from engine.replay.flywheel_runner import FlywheelEvidenceManifest, FlywheelRunner
from engine.replication.genesis_factory import GenesisFactory
from engine.replication.models import AuthorizationDecision
from engine.replication.replication_manager import ReplicationManager
from engine.replication.services.human_authorization_service import HumanAuthorizationService
from engine.risk.governor import RiskGovernor
from engine.risk.models import MarketMark, OperationalState, TransitionReason
from engine.risk.state_machine import RiskStateMachine
from engine.siphon.models import SyntheticSettlementMetadata
from engine.siphon.siphon_manager import SiphonManager
from engine.treasury.treasury_manager import TreasuryManager


pytestmark = pytest.mark.integration
SESSION_N_OPEN = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)
SESSION_N_CLOSE = datetime(2026, 9, 8, 20, 0, tzinfo=UTC)
SESSION_N1_OPEN = datetime(2026, 9, 9, 13, 30, tzinfo=UTC)
SESSION_N1_CLOSE = datetime(2026, 9, 9, 20, 0, tzinfo=UTC)
EXPECTED_FLYWHEEL_SHA256 = "245f4038e6b29e800778f5470b272da9973f097bb3cb89b57ef93b8f2b0c0fb3"


def uid(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"kairo-step4:{value}")


def identity(record_type: str, stable_key: str) -> UUID:
    return uid(f"{record_type}:{stable_key}")


@dataclass
class RecordingEngine:
    calls: list[tuple[datetime, bool]] = field(default_factory=list)

    def step(self, timestamp: datetime, _market_slice: object, *, can_trade: bool) -> None:
        self.calls.append((timestamp, can_trade))


@dataclass
class FlywheelCase:
    runner: FlywheelRunner
    manifest: FlywheelEvidenceManifest
    a001: CapitalCell
    a002: CapitalCell
    instrument: Instrument
    broker: BrokerAccount
    engines: dict[str, RecordingEngine]
    feed: list[dict]


def add_profit(
    session: Session,
    *,
    cell: CapitalCell,
    broker: BrokerAccount,
    instrument: Instrument,
    amount: Decimal,
    at: datetime,
) -> Fill:
    snapshot = MarketSnapshot(
        snapshot_id=uid(f"fill-snapshot:{cell.cell_code}"),
        instrument_id=instrument.instrument_id,
        captured_at=at,
        bid=Decimal("99.99"),
        ask=Decimal("100.00"),
        last=Decimal("100.00"),
        payload={"synthetic": True},
    )
    intent = OrderIntent(
        intent_id=uid(f"intent:{cell.cell_code}"),
        cell_id=cell.cell_id,
        strategy_id=cell.strategy_id,
        strategy_version=cell.strategy_version,
        instrument_id=instrument.instrument_id,
        client_order_key=f"step4-close-{cell.cell_code}",
        order_purpose="TAKE_PROFIT",
        side="SELL",
        target_notional_usd=None,
        target_quantity=Decimal("1"),
        order_type="MARKET",
        created_at=at,
    )
    order = KairoOrder(
        kairo_order_id=uid(f"order:{cell.cell_code}"),
        intent_id=intent.intent_id,
        risk_decision_id=None,
        broker_account_id=broker.broker_account_id,
        broker_order_id=f"STEP4-{cell.cell_code}",
        status="FILLED",
        submitted_at=at,
    )
    fill = Fill(
        fill_id=uid(f"fill:{cell.cell_code}"),
        kairo_order_id=order.kairo_order_id,
        broker_account_id=broker.broker_account_id,
        broker_fill_id=f"STEP4-FILL-{cell.cell_code}",
        instrument_id=instrument.instrument_id,
        side="SELL",
        quantity=Decimal("1"),
        price=Decimal("100"),
        reference_price=Decimal("100"),
        contract_multiplier=Decimal("1"),
        slippage_usd=Decimal("0"),
        commission_fee_usd=Decimal("0"),
        is_simulated=True,
        liquidity_fidelity_tier="TIER_1_QUOTE_DEPTH",
        simulation_model="STEP3-PAPER-FILL",
        simulation_policy_version="1.0.0",
        source_snapshot_id=snapshot.snapshot_id,
        simulation_metadata={"synthetic": "true", "execution_guaranteed": False},
        filled_at=at,
    )
    session.add_all([snapshot, intent])
    session.flush()
    session.add(order)
    session.flush()
    session.add(fill)
    session.flush()
    SiphonManager(session, identity_factory=identity).record_canonical_realized_pnl(
        fill_id=fill.fill_id,
        cell_id=cell.cell_id,
        position_effect="CLOSING",
        realized_pnl_usd=amount,
        occurred_at=at,
    )
    return fill


def qualify(session: Session, cell: CapitalCell, amount: Decimal, at: datetime) -> None:
    result = SiphonManager(session, identity_factory=identity).qualify_and_allocate(
        cell_id=cell.cell_id,
        occurred_at=at,
        synthetic_settled_cash_usd=Decimal("100") + amount,
        synthetic_settlement_metadata=SyntheticSettlementMetadata(
            synthetic_settled_at=at,
            replay_session_id=f"STEP4-{cell.cell_code}",
            model_version="SETTLEMENT-SIM-v0.1",
        ),
    )
    assert result is not None and result.qualified_profit_usd == amount


def execute_treasury(
    session: Session,
    *,
    cell: CapitalCell,
    instrument: Instrument,
    at: datetime,
) -> None:
    config = session.scalar(select(CellTreasuryConfig).where(
        CellTreasuryConfig.cell_id == cell.cell_id,
        CellTreasuryConfig.is_active.is_(True),
    ))
    snapshot = MarketSnapshot(
        snapshot_id=uid(f"treasury-snapshot:{cell.cell_code}"),
        instrument_id=instrument.instrument_id,
        captured_at=at,
        bid=Decimal("99.99"),
        ask=Decimal("100.00"),
        last=Decimal("100.00"),
        payload={"luld_halted": False, "market_open": True, "regular_session": True},
    )
    session.add(snapshot)
    session.flush()
    rows = TreasuryManager(session, identity_factory=identity).execute_available(
        cell_id=cell.cell_id,
        is_synthetic=True,
        market_snapshot_ids={config.config_id: snapshot.snapshot_id},
        occurred_at=at,
    )
    assert len(rows) == 1


def build_case(session: Session) -> FlywheelCase:
    strategy = session.get(StrategyRegistry, ("EMA-CROSS-001", "1.0.0"))
    assert strategy is not None
    instrument = Instrument(
        instrument_id=uid("instrument:META"), symbol="META-STEP4", asset_class="EQUITY",
        currency="USD", effective_from=SESSION_N_OPEN,
    )
    broker = BrokerAccount(
        broker_account_id=uid("broker"), account_key="STEP4-PAPER", broker_name="TEST",
        environment="PAPER", status="ACTIVE", effective_from=SESSION_N_OPEN,
    )
    a001 = CapitalCell(
        cell_id=uid("cell:A001"), cell_code="A001", seed_capital=Decimal("100"),
        status="ACTIVE", autonomy_tier="APPRENTICE", strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag, target_treasury_code=instrument.symbol,
        economic_domain="SYNTHETIC", updated_at=SESSION_N_OPEN,
    )
    session.add_all([instrument, broker, a001])
    session.flush()
    config = CellTreasuryConfig(
        config_id=uid("target:A001"), cell_id=a001.cell_id, target_type="SINGLE_ASSET",
        target_instrument_id=instrument.instrument_id, target_symbol=instrument.symbol,
        config_version=1, is_active=True, authorized_by="OWNER", created_at=SESSION_N_OPEN,
    )
    provenance = SyntheticEvidenceManifest(
        manifest_id=uid("provenance:A001"), manifest_type="REPLAY_RUN",
        manifest_hash="1" * 64, manifest_algorithm="REPLAY-MANIFEST-v1",
        cell_id=a001.cell_id, source_count=1, source_refs={"session": "STEP4-SEED"},
        model_identifier="KAIRO-REPLAY", model_version="1.0.0", created_at=SESSION_N_OPEN,
    )
    session.add_all([config, provenance])
    session.flush()
    session.add(KairoCapitalAuthorizationRecord(
        authorization_id=uid("capital:A001"), cell_id=a001.cell_id,
        broker_snapshot_id=None, broker_account_id=None, economic_domain="SYNTHETIC",
        synthetic_provenance_id=provenance.manifest_id, settled_cash=Decimal("600"),
        safety_reserve=Decimal("0"), ownership_treasury_reserved=Decimal("0"),
        replication_reserve=Decimal("0"), committed_obligations=Decimal("0"),
        authorized_trading_cash=Decimal("600"), computed_at=SESSION_N_OPEN,
    ))
    session.flush()

    add_profit(session, cell=a001, broker=broker, instrument=instrument,
               amount=Decimal("500"), at=SESSION_N_OPEN + timedelta(minutes=5))
    qualify(session, a001, Decimal("500"), SESSION_N_OPEN + timedelta(minutes=6))
    execute_treasury(session, cell=a001, instrument=instrument,
                     at=SESSION_N_OPEN + timedelta(minutes=7))
    proposal = ReplicationManager(session).create_proposal(
        parent_cell_id=a001.cell_id, proposed_child_code="A002", is_synthetic=True,
        occurred_at=SESSION_N_OPEN + timedelta(minutes=8),
    )
    assert proposal is not None
    HumanAuthorizationService(session).authorize_proposal(
        proposal_id=proposal.proposal_id, manifest_hash=proposal.manifest_hash,
        decision=AuthorizationDecision.APPROVE, authorized_by="OWNER",
        authorization_method="SIGNED_REVIEW",
        authorized_at=SESSION_N_OPEN + timedelta(minutes=9),
    )
    a002 = GenesisFactory(session).instantiate_child_cell(
        proposal_id=proposal.proposal_id,
        occurred_at=SESSION_N_OPEN + timedelta(minutes=10),
    )

    add_profit(session, cell=a002, broker=broker, instrument=instrument,
               amount=Decimal("20"), at=SESSION_N1_OPEN + timedelta(minutes=5))
    qualify(session, a002, Decimal("20"), SESSION_N1_OPEN + timedelta(minutes=6))
    execute_treasury(session, cell=a002, instrument=instrument,
                     at=SESSION_N1_OPEN + timedelta(minutes=7))

    engines = {"A001": RecordingEngine(), "A002": RecordingEngine()}
    feed = [
        {
            "session_id": "SESSION-N", "session_open": SESSION_N_OPEN,
            "session_close": SESSION_N_CLOSE,
            "timestamp": SESSION_N_OPEN + timedelta(hours=1),
            "arm_cells": ["A001", "A002"], "cell_engines": engines,
            "market_slice": {"sequence": 1},
        },
        {
            "session_id": "SESSION-N+1", "session_open": SESSION_N1_OPEN,
            "session_close": SESSION_N1_CLOSE,
            "timestamp": SESSION_N1_OPEN + timedelta(hours=1),
            "arm_cells": ["A001", "A002"], "cell_engines": engines,
            "market_slice": {"sequence": 2},
        },
    ]
    runner = FlywheelRunner(session)
    manifest = runner.run_replay(feed)
    return FlywheelCase(runner, manifest, a001, a002, instrument, broker, engines, feed)


def test_flywheel_qualified_profit_reconciles_exactly_to_40_40_20_allocations(db_session: Session) -> None:
    case = build_case(db_session)
    assert case.manifest.siphon_reconciliation["cells"]["A001"] | {
        "qualified_profit_usd": "500.00", "safety_allocated_usd": "200.00",
        "treasury_allocated_usd": "200.00", "replication_allocated_usd": "100.00",
    } == case.manifest.siphon_reconciliation["cells"]["A001"]
    assert case.manifest.siphon_reconciliation["cells"]["A002"]["qualified_profit_usd"] == "20.00"


def test_treasury_allocations_reconcile_consumed_plus_unconsumed(db_session: Session) -> None:
    cells = build_case(db_session).manifest.siphon_reconciliation["cells"]
    for row in cells.values():
        assert Decimal(row["treasury_allocated_usd"]) == (
            Decimal(row["treasury_consumed_usd"]) + Decimal(row["treasury_unconsumed_usd"])
        )


def test_replication_allocations_reconcile_consumed_reserved_and_available(db_session: Session) -> None:
    cells = build_case(db_session).manifest.siphon_reconciliation["cells"]
    for row in cells.values():
        assert Decimal(row["replication_allocated_usd"]) == sum((
            Decimal(row["replication_consumed_usd"]),
            Decimal(row["replication_active_reserved_usd"]),
            Decimal(row["replication_uncommitted_usd"]),
        ))


def test_cumulative_siphoned_profit_never_exceeds_net_settled_realized_profit(db_session: Session) -> None:
    cells = build_case(db_session).manifest.siphon_reconciliation["cells"]
    assert all(Decimal(row["qualified_profit_usd"]) <= Decimal(row["net_settled_realized_pnl_usd"])
               for row in cells.values())


def test_genesis_child_does_not_trade_during_birth_session(db_session: Session) -> None:
    case = build_case(db_session)
    assert [at for at, _ in case.engines["A002"].calls] == [SESSION_N1_OPEN + timedelta(hours=1)]


def test_child_first_eligible_session_starts_disarmed_and_requires_explicit_arm(db_session: Session) -> None:
    case = build_case(db_session)
    events = list(db_session.scalars(select(RiskStateEvent).where(
        RiskStateEvent.cell_id == case.a002.cell_id,
        RiskStateEvent.session_id == "SESSION-N+1:A002",
    ).order_by(RiskStateEvent.recorded_at, RiskStateEvent.event_id)))
    assert [event.new_state for event in events] == ["DISARMED", "ARMED"]
    assert events[0].current_session_net_pnl == Decimal("0")
    assert case.engines["A002"].calls[0][1] is True


def test_multicell_replay_uses_deterministic_cell_order_not_runtime_concurrency(db_session: Session) -> None:
    case = build_case(db_session)
    assert [code for at, code in case.runner.processing_trace if at.date() == SESSION_N1_OPEN.date()] == ["A001", "A002"]


def test_a001_loss_halt_does_not_halt_a002(db_session: Session) -> None:
    case = build_case(db_session)
    state_a001 = db_session.get(RiskGovernorState, case.a001.cell_id)
    RiskStateMachine(db_session, cell_id=case.a001.cell_id).transition(
        state_a001, OperationalState.HALTED_HARD, TransitionReason.SESSION_LOSS_LIMIT,
        Decimal("600"),
    )
    assert db_session.get(RiskGovernorState, case.a001.cell_id).operational_state == "HALTED_HARD"
    assert db_session.get(RiskGovernorState, case.a002.cell_id).operational_state == "ARMED"


def test_a001_market_marks_do_not_change_a002_pnl(db_session: Session) -> None:
    case = build_case(db_session)
    db_session.add(CurrentPosition(
        position_id=uid("position:A001"), cell_id=case.a001.cell_id,
        broker_account_id=case.broker.broker_account_id,
        instrument_id=case.instrument.instrument_id, quantity=Decimal("1"),
        average_price=Decimal("90"), updated_at=SESSION_N1_OPEN,
    ))
    db_session.flush()
    before = db_session.get(RiskGovernorState, case.a002.cell_id).session_net_pnl
    clock = VirtualClock(SESSION_N1_OPEN + timedelta(hours=2))
    RiskGovernor(
        db_session, cell_id=case.a001.cell_id, clock=clock,
        identities=ReplayIdentityFactory("STEP4-MARK-A001"),
    ).record_market_mark(
        MarketMark(
            instrument_id=case.instrument.instrument_id, mark_price=Decimal("110"),
            source_timestamp=clock.now(), received_at=clock.now(),
        ),
        positions=[], authorized_cash_usd=Decimal("600"),
    )
    assert db_session.get(RiskGovernorState, case.a002.cell_id).session_net_pnl == before


def test_a002_profit_routes_only_to_a002_siphon_allocations(db_session: Session) -> None:
    case = build_case(db_session)
    rows = db_session.execute(
        select(SiphonEvent.cell_id, func.sum(SiphonAllocation.allocated_usd))
        .join(SiphonAllocation).group_by(SiphonEvent.cell_id)
    ).all()
    assert dict(rows) == {case.a001.cell_id: Decimal("500.00"), case.a002.cell_id: Decimal("20.00")}


def test_a001_and_a002_treasury_executions_remain_cell_isolated(db_session: Session) -> None:
    case = build_case(db_session)
    rows = list(db_session.scalars(select(TreasuryExecution).order_by(TreasuryExecution.cell_id)))
    assert {row.cell_id for row in rows} == {case.a001.cell_id, case.a002.cell_id}
    assert all(db_session.get(CellTreasuryConfig, row.target_config_id).cell_id == row.cell_id for row in rows)


def test_holdings_for_both_cells_rebuild_exactly_from_immutable_treasury_ledgers(db_session: Session) -> None:
    case = build_case(db_session)
    expected = {
        (row.cell_id, row.instrument_id): (Decimal(row.shares_executed), Decimal(row.net_amount_usd))
        for row in db_session.scalars(select(TreasuryExecution))
    }
    db_session.execute(delete(OwnershipTreasuryHolding))
    for cell in (case.a001, case.a002):
        TreasuryManager(db_session, identity_factory=identity).rebuild_holdings_projection(
            cell_id=cell.cell_id, is_synthetic=True
        )
    actual = {
        (row.cell_id, row.instrument_id): (Decimal(row.total_shares), Decimal(row.cumulative_cost_basis_usd))
        for row in db_session.scalars(select(OwnershipTreasuryHolding))
    }
    assert actual == expected


def test_identical_full_flywheel_replays_produce_identical_financial_manifest(db_session: Session) -> None:
    case = build_case(db_session)
    first = case.manifest
    second = case.runner.run_replay(case.feed)
    assert second.model_dump(mode="json") == first.model_dump(mode="json")
    assert second.compute_sha256_hash() == first.manifest_hash
    assert first.manifest_hash == EXPECTED_FLYWHEEL_SHA256


def test_full_flywheel_has_zero_orphan_orders_fills_consumptions_or_reservations(db_session: Session) -> None:
    case = build_case(db_session)
    assert case.runner._has_zero_orphans() is True
    assert db_session.scalar(select(func.count()).select_from(Fill)) == 2
    assert db_session.scalar(select(func.count()).select_from(TreasuryCashConsumption)) == 2
    assert db_session.scalar(select(func.count()).select_from(ReplicationCashConsumption)) == 1
    assert db_session.scalar(select(func.count()).select_from(ReplicationProposalReservation)) == 1


def test_full_flywheel_preserves_exact_cent_conservation(db_session: Session) -> None:
    manifest = build_case(db_session).manifest
    assert manifest.conservation_proof_passed is True
    assert manifest.siphon_reconciliation["global"] == {
        "qualified_profit_usd": "520.00",
        "net_settled_realized_pnl_usd": "520.00",
        "safety_allocated_usd": "208.00",
        "treasury_allocated_usd": "208.00",
        "replication_allocated_usd": "104.00",
    }
