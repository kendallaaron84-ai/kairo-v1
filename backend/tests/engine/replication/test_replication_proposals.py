import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, inspect, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.db.models.broker import BrokerAccount
from app.db.models.configuration import CellTreasuryConfig, Instrument, StrategyRegistry
from app.db.models.ledger import BrokerCashSnapshot, SiphonAllocation, SiphonEvent
from app.db.models.projections import CapitalCell
from app.db.models.replication import (
    CellReplicationProposal,
    ReplicationProposalEvent,
    ReplicationProposalReservation,
    ReplicationReservationEvent,
)
from engine.replication.models import (
    AllocationReservationRef,
    ReplicationProposalManifest,
)
from engine.replication.replication_manager import ReplicationManager


pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@dataclass
class Context:
    cell: CapitalCell
    target: CellTreasuryConfig
    broker: BrokerAccount


def seed_context(session: Session, code: str | None = None) -> Context:
    strategy = session.get(StrategyRegistry, ("EMA-CROSS-001", "1.0.0"))
    assert strategy is not None
    broker = BrokerAccount(
        broker_account_id=uuid4(), account_key=f"rep-{uuid4()}", broker_name="TEST",
        environment="PAPER", status="ACTIVE", effective_from=NOW,
    )
    instrument = Instrument(
        instrument_id=uuid4(), symbol=f"REP{uuid4().hex[:8]}", asset_class="EQUITY",
        currency="USD", effective_from=NOW,
    )
    session.add_all([broker, instrument])
    session.flush()
    owner = CapitalCell(
        cell_id=uuid4(), cell_code=code or f"R-{uuid4().hex[:8]}",
        seed_capital=Decimal("100"), status="ACTIVE", autonomy_tier="APPRENTICE",
        strategy_id=strategy.strategy_id, strategy_version=strategy.version_tag,
        target_treasury_code=instrument.symbol, economic_domain="SYNTHETIC",
    )
    session.add(owner)
    session.flush()
    target = CellTreasuryConfig(
        config_id=uuid4(), cell_id=owner.cell_id, target_type="SINGLE_ASSET",
        target_instrument_id=instrument.instrument_id, target_symbol=instrument.symbol,
        config_version=1, is_active=True, authorized_by="TEST", created_at=NOW,
    )
    session.add(target)
    session.flush()
    return Context(owner, target, broker)


def allocation(
    session: Session,
    context: Context,
    amount: str,
    *,
    synthetic: bool = True,
    bucket: str = "REPLICATION_POOL",
) -> SiphonAllocation:
    snapshot = None
    if not synthetic:
        snapshot = BrokerCashSnapshot(
            snapshot_id=uuid4(), broker_account_id=context.broker.broker_account_id,
            broker_cash=Decimal("1000"), settled_cash=Decimal("1000"),
            unsettled_cash=Decimal("0"), buying_power=Decimal("1000"),
            currency="USD", captured_at=NOW,
        )
        session.add(snapshot)
        session.flush()
    value = Decimal(amount)
    siphon = SiphonEvent(
        siphon_id=uuid4(), cell_id=context.cell.cell_id,
        treasury_code=context.target.target_symbol, amount=value,
        occurred_at=NOW, reason_code="TEST", policy_id="PROFIT-ALLOC-v1.0",
        policy_version="1.0.0",
        broker_account_id=None if synthetic else context.broker.broker_account_id,
        settlement_snapshot_id=None if synthetic else snapshot.snapshot_id,
        source_fill_ids=[], qualified_profit_usd=value,
        safety_reserve_usd=Decimal("0"), target_treasury_usd=Decimal("0"),
        replication_pool_usd=value, target_config_id=context.target.config_id,
        is_synthetic=synthetic,
        synthetic_settlement_metadata=(
            {"settlement_evidence_type": "SYNTHETIC_REPLAY_SETTLEMENT",
             "synthetic_settled_at": NOW.isoformat(), "replay_session_id": "STEP3A",
             "model_version": "SETTLEMENT-SIM-v0.1"}
            if synthetic else None
        ),
        source_manifest_hash="a" * 64,
    )
    session.add(siphon)
    session.flush()
    row = SiphonAllocation(
        allocation_id=uuid4(), siphon_id=siphon.siphon_id, bucket_type=bucket,
        allocated_usd=value, unallocated_cash_balance_usd=Decimal("0"), occurred_at=NOW,
    )
    session.add(row)
    session.flush()
    return row


def raw_proposal(session: Session, context: Context, suffix: str) -> CellReplicationProposal:
    row = CellReplicationProposal(
        proposal_id=uuid4(), parent_cell_id=context.cell.cell_id,
        proposed_child_code=f"C-{suffix}-{uuid4().hex[:4]}"[:16],
        capital_class="MICRO-100-v1", proposed_seed_capital_usd=Decimal("100"),
        strategy_identifier=context.cell.strategy_id,
        strategy_version=context.cell.strategy_version,
        risk_policy_identifier="RISK-v0.1", target_config_id=context.target.config_id,
        proposed_autonomy_tier="APPRENTICE", is_synthetic=True,
        manifest_hash=hashlib.sha256(f"{suffix}-{uuid4()}".encode()).hexdigest(),
        created_at=NOW,
    )
    session.add(row)
    session.flush()
    return row


def pending_event(session: Session, proposal: CellReplicationProposal) -> None:
    session.add(ReplicationProposalEvent(
        event_id=uuid4(), proposal_id=proposal.proposal_id, state_from="INITIAL",
        state_to="PENDING_AUTHORIZATION", reason_code="TEST", occurred_at=NOW,
    ))
    session.flush()


def raw_reservation(
    session: Session, proposal: CellReplicationProposal, source: SiphonAllocation,
    *, amount: str = "1",
) -> ReplicationProposalReservation:
    row = ReplicationProposalReservation(
        reservation_id=uuid4(), proposal_id=proposal.proposal_id,
        allocation_id=source.allocation_id, reserved_usd=Decimal(amount), occurred_at=NOW,
    )
    session.add(row)
    session.flush()
    return row


def reservation_event(
    session: Session, reservation: ReplicationProposalReservation,
    event_type: str, occurred_at: datetime = NOW,
) -> ReplicationReservationEvent:
    row = ReplicationReservationEvent(
        event_id=uuid4(), reservation_id=reservation.reservation_id,
        event_type=event_type, reason_code="TEST", occurred_at=occurred_at,
    )
    session.add(row)
    session.flush()
    return row


def test_database_rejects_live_replication_proposal_in_phase4(db_session: Session) -> None:
    context = seed_context(db_session)
    live = CellReplicationProposal(
        proposal_id=uuid4(), parent_cell_id=context.cell.cell_id,
        proposed_child_code=f"L-{uuid4().hex[:8]}", capital_class="MICRO-100-v1",
        proposed_seed_capital_usd=Decimal("100"),
        strategy_identifier=context.cell.strategy_id, strategy_version=context.cell.strategy_version,
        risk_policy_identifier="RISK-v0.1", target_config_id=context.target.config_id,
        proposed_autonomy_tier="APPRENTICE", is_synthetic=False,
        manifest_hash=hashlib.sha256(str(uuid4()).encode()).hexdigest(), created_at=NOW,
    )
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(live)
        db_session.flush()


def test_runtime_cannot_emit_authorized_or_executed_proposal_state(
    db_session: Session,
) -> None:
    context = seed_context(db_session)
    for state in ("AUTHORIZED", "EXECUTED"):
        proposal = raw_proposal(db_session, context, state)
        pending_event(db_session, proposal)
        with pytest.raises(DBAPIError, match="not authorized|Unauthorized"), db_session.begin_nested():
            db_session.add(ReplicationProposalEvent(
                event_id=uuid4(), proposal_id=proposal.proposal_id,
                state_from="PENDING_AUTHORIZATION", state_to=state,
                reason_code="TEST", occurred_at=NOW + timedelta(seconds=1),
            ))
            db_session.flush()


def test_proposal_lifecycle_rejects_unknown_or_invalid_transitions(db_session: Session) -> None:
    proposal = raw_proposal(db_session, seed_context(db_session), "INVALID")
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(ReplicationProposalEvent(
            event_id=uuid4(), proposal_id=proposal.proposal_id,
            state_from="BOGUS", state_to="PENDING_AUTHORIZATION",
            reason_code="TEST", occurred_at=NOW,
        ))
        db_session.flush()
    with pytest.raises(DBAPIError, match="First proposal event"), db_session.begin_nested():
        db_session.add(ReplicationProposalEvent(
            event_id=uuid4(), proposal_id=proposal.proposal_id,
            state_from="PENDING_AUTHORIZATION", state_to="CANCELLED",
            reason_code="TEST", occurred_at=NOW,
        ))
        db_session.flush()


def test_reservation_first_event_must_be_reserved(db_session: Session) -> None:
    context = seed_context(db_session)
    source = allocation(db_session, context, "10")
    reservation = raw_reservation(db_session, raw_proposal(db_session, context, "FIRST"), source)
    with pytest.raises(DBAPIError, match="First reservation event"), db_session.begin_nested():
        reservation_event(db_session, reservation, "RELEASED")


def test_reservation_event_type_rejects_unknown_values(db_session: Session) -> None:
    context = seed_context(db_session)
    reservation = raw_reservation(
        db_session, raw_proposal(db_session, context, "TYPE"), allocation(db_session, context, "10")
    )
    with pytest.raises(IntegrityError), db_session.begin_nested():
        reservation_event(db_session, reservation, "UNKNOWN")


def test_reservation_event_time_cannot_move_backward(db_session: Session) -> None:
    context = seed_context(db_session)
    reservation = raw_reservation(
        db_session, raw_proposal(db_session, context, "TIME"), allocation(db_session, context, "10")
    )
    reservation_event(db_session, reservation, "RESERVED")
    with pytest.raises(DBAPIError, match="cannot move backward"), db_session.begin_nested():
        reservation_event(db_session, reservation, "RELEASED", NOW - timedelta(seconds=1))


def test_proposal_and_reservation_lifecycle_events_are_append_only(db_session: Session) -> None:
    context = seed_context(db_session)
    source = allocation(db_session, context, "100")
    proposal = raw_proposal(db_session, context, "IMMUTABLE")
    pending_event(db_session, proposal)
    reservation = raw_reservation(db_session, proposal, source)
    reservation_event(db_session, reservation, "RESERVED")
    models = (CellReplicationProposal, ReplicationProposalEvent,
              ReplicationProposalReservation, ReplicationReservationEvent)
    for model in models:
        with pytest.raises(DBAPIError, match="Immutable fact"), db_session.begin_nested():
            db_session.execute(delete(model))
            db_session.flush()


def test_phase4_replication_manager_does_not_create_live_genesis_proposal(db_session: Session) -> None:
    context = seed_context(db_session)
    allocation(db_session, context, "100", synthetic=False)
    result = ReplicationManager(db_session).create_proposal(
        parent_cell_id=context.cell.cell_id, proposed_child_code="A002",
        is_synthetic=False, occurred_at=NOW,
    )
    assert result is None
    assert db_session.scalar(select(CellReplicationProposal)) is None


def test_reservation_terminal_states_cannot_be_reactivated(db_session: Session) -> None:
    context = seed_context(db_session)
    reservation = raw_reservation(
        db_session, raw_proposal(db_session, context, "TERMINAL"), allocation(db_session, context, "10")
    )
    reservation_event(db_session, reservation, "RESERVED")
    reservation_event(db_session, reservation, "RELEASED", NOW + timedelta(seconds=1))
    with pytest.raises(DBAPIError, match="terminal state"), db_session.begin_nested():
        reservation_event(db_session, reservation, "RESERVED", NOW + timedelta(seconds=2))


def test_replication_cash_uses_only_replication_pool_allocations(db_session: Session) -> None:
    context = seed_context(db_session)
    allocation(db_session, context, "90")
    allocation(db_session, context, "50", bucket="TARGET_TREASURY")
    assert ReplicationManager(db_session).available_replication_cash(
        cell_id=context.cell.cell_id, is_synthetic=True
    ) == Decimal("90")


def test_replication_threshold_evaluated_independently_per_domain(db_session: Session) -> None:
    context = seed_context(db_session)
    allocation(db_session, context, "60", synthetic=True)
    allocation(db_session, context, "70", synthetic=False)
    manager = ReplicationManager(db_session)
    assert manager.available_replication_cash(cell_id=context.cell.cell_id, is_synthetic=True) == 60
    assert manager.available_replication_cash(cell_id=context.cell.cell_id, is_synthetic=False) == 70


def test_synthetic_and_live_replication_cash_never_combine(db_session: Session) -> None:
    context = seed_context(db_session)
    allocation(db_session, context, "60", synthetic=True)
    allocation(db_session, context, "60", synthetic=False)
    manager = ReplicationManager(db_session)
    assert manager.create_proposal(
        parent_cell_id=context.cell.cell_id, proposed_child_code="A002",
        is_synthetic=True, occurred_at=NOW,
    ) is None


def test_reaching_threshold_creates_proposal_and_reservation_facts(db_session: Session) -> None:
    context = seed_context(db_session)
    allocation(db_session, context, "100")
    proposal = ReplicationManager(db_session).create_proposal(
        parent_cell_id=context.cell.cell_id, proposed_child_code="A002",
        is_synthetic=True, occurred_at=NOW,
    )
    assert proposal is not None and proposal.is_synthetic is True
    event = db_session.scalar(select(ReplicationProposalEvent).where(
        ReplicationProposalEvent.proposal_id == proposal.proposal_id
    ))
    reservation = db_session.scalar(select(ReplicationProposalReservation).where(
        ReplicationProposalReservation.proposal_id == proposal.proposal_id
    ))
    assert event.state_to == "PENDING_AUTHORIZATION"
    assert reservation.reserved_usd == Decimal("100")
    assert db_session.scalar(select(ReplicationReservationEvent).where(
        ReplicationReservationEvent.reservation_id == reservation.reservation_id
    )).event_type == "RESERVED"


def test_active_reservations_reduce_available_replication_cash(db_session: Session) -> None:
    context = seed_context(db_session)
    allocation(db_session, context, "125")
    manager = ReplicationManager(db_session)
    manager.create_proposal(
        parent_cell_id=context.cell.cell_id, proposed_child_code="A002",
        is_synthetic=True, occurred_at=NOW,
    )
    assert manager.available_replication_cash(
        cell_id=context.cell.cell_id, is_synthetic=True
    ) == Decimal("25")


def test_concurrent_proposals_cannot_reserve_same_allocation_dollars(
    migrated_database: tuple[str, str],
) -> None:
    admin_url, _ = migrated_database
    engine = create_engine(admin_url)
    with Session(engine) as setup:
        context = seed_context(setup, f"CC-{uuid4().hex[:6]}")
        allocation(setup, context, "100")
        cell_id = context.cell.cell_id
        config_id = context.target.config_id
        instrument_id = context.target.target_instrument_id
        broker_id = context.broker.broker_account_id
        setup.commit()

    def attempt(code: str):
        with Session(engine) as session:
            result = ReplicationManager(session).create_proposal(
                parent_cell_id=cell_id, proposed_child_code=code,
                is_synthetic=True, occurred_at=NOW,
            )
            session.commit()
            return result is not None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (f"C{uuid4().hex[:8]}", f"C{uuid4().hex[:8]}")))
    assert sorted(results) == [False, True]
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "TRUNCATE TABLE replication_reservation_events, "
            "replication_proposal_reservations, replication_proposal_events, "
            "cell_replication_proposals, siphon_allocations, siphon_events CASCADE"
        )
        connection.exec_driver_sql(
            "DELETE FROM cell_treasury_configs WHERE config_id = %s", (config_id,)
        )
        connection.exec_driver_sql(
            "DELETE FROM capital_cells WHERE cell_id = %s", (cell_id,)
        )
        connection.exec_driver_sql(
            "DELETE FROM instruments WHERE instrument_id = %s", (instrument_id,)
        )
        connection.exec_driver_sql(
            "DELETE FROM broker_accounts WHERE broker_account_id = %s", (broker_id,)
        )
    engine.dispose()


def test_database_trigger_rejects_over_reservation_beyond_allocation_balance(db_session: Session) -> None:
    context = seed_context(db_session)
    source = allocation(db_session, context, "10")
    first = raw_reservation(db_session, raw_proposal(db_session, context, "OVER1"), source, amount="8")
    reservation_event(db_session, first, "RESERVED")
    second = raw_reservation(db_session, raw_proposal(db_session, context, "OVER2"), source, amount="3")
    with pytest.raises(DBAPIError, match="exceeds allocation"), db_session.begin_nested():
        reservation_event(db_session, second, "RESERVED")


def test_database_trigger_rejects_reservation_on_non_replication_pool_bucket(db_session: Session) -> None:
    context = seed_context(db_session)
    source = allocation(db_session, context, "10", bucket="TARGET_TREASURY")
    reservation = raw_reservation(db_session, raw_proposal(db_session, context, "BUCKET"), source)
    with pytest.raises(DBAPIError, match="strictly on REPLICATION_POOL"), db_session.begin_nested():
        reservation_event(db_session, reservation, "RESERVED")


def test_canonical_proposal_manifest_hash_is_deterministic_and_byte_exact() -> None:
    manifest = ReplicationProposalManifest(
        manifest_algorithm="REPLICATION-PROPOSAL-v1",
        parent_cell_id=UUID("10000000-0000-4000-8000-000000000001"),
        proposed_child_code="A002", capital_class="MICRO-100-v1",
        proposed_seed_capital_usd=Decimal("100.00"),
        strategy_identifier="EMA-CROSS-001", strategy_version="1.0.0",
        risk_policy_identifier="RISK-v0.1",
        target_config_id=UUID("20000000-0000-4000-8000-000000000002"),
        proposed_autonomy_tier="APPRENTICE", is_synthetic=True, created_at=NOW,
        source_allocations=(AllocationReservationRef(
            allocation_id=UUID("30000000-0000-4000-8000-000000000003"),
            reserved_usd=Decimal("100.00"),
        ),),
    )
    expected = (
        b'{"capital_class":"MICRO-100-v1","created_at":"2026-09-01T12:00:00Z",'
        b'"is_synthetic":true,"manifest_algorithm":"REPLICATION-PROPOSAL-v1",'
        b'"parent_cell_id":"10000000-0000-4000-8000-000000000001",'
        b'"proposed_autonomy_tier":"APPRENTICE","proposed_child_code":"A002",'
        b'"proposed_seed_capital_usd":"100.00","risk_policy_identifier":"RISK-v0.1",'
        b'"source_allocations":[{"allocation_id":"30000000-0000-4000-8000-000000000003",'
        b'"reserved_usd":"100.00"}],"strategy_identifier":"EMA-CROSS-001",'
        b'"strategy_version":"1.0.0","target_config_id":"20000000-0000-4000-8000-000000000002"}'
    )
    assert manifest.canonical_bytes() == expected
    assert manifest.sha256() == hashlib.sha256(expected).hexdigest()


def test_releasing_reservation_restores_available_replication_cash(db_session: Session) -> None:
    context = seed_context(db_session)
    allocation(db_session, context, "100")
    manager = ReplicationManager(db_session)
    proposal = manager.create_proposal(
        parent_cell_id=context.cell.cell_id, proposed_child_code="A002",
        is_synthetic=True, occurred_at=NOW,
    )
    assert manager.available_replication_cash(cell_id=context.cell.cell_id, is_synthetic=True) == 0
    manager.release_proposal(proposal_id=proposal.proposal_id, occurred_at=NOW + timedelta(seconds=1))
    assert manager.available_replication_cash(cell_id=context.cell.cell_id, is_synthetic=True) == 100


def test_migration_0014_upgrade_and_downgrade_are_clean_and_data_safe(
    migrated_database: tuple[str, str],
) -> None:
    admin_url, _ = migrated_database
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    engine = create_engine(admin_url)
    command.downgrade(config, "0013")
    assert "cell_replication_proposals" not in inspect(engine).get_table_names()
    command.upgrade(config, "0014")
    assert "cell_replication_proposals" in inspect(engine).get_table_names()
    command.downgrade(config, "0013")
    command.upgrade(config, "head")
    engine.dispose()
