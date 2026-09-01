from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.models.broker import BrokerAccount
from app.db.models.configuration import CellTreasuryConfig, Instrument, StrategyRegistry
from app.db.models.ledger import (
    KairoCapitalAuthorizationRecord,
    SiphonAllocation,
    SiphonEvent,
    SyntheticEvidenceManifest,
)
from app.db.models.projections import CapitalCell
from app.db.models.replication import (
    CellGenesisEvent,
    ReplicationAuthorization,
    ReplicationCashConsumption,
    ReplicationProposalEvent,
    ReplicationProposalReservation,
    ReplicationReservationEvent,
)
from app.db.models.risk import RiskGovernorState, RiskSession
from engine.replication.genesis_factory import GenesisFactory
from engine.replication.models import (
    AuthorizationDecision,
    GenesisAllocationSource,
    GenesisSeedManifest,
)
from engine.replication.replication_manager import ReplicationManager
from engine.replication.services.human_authorization_service import HumanAuthorizationService
from engine.risk.models import RiskSessionSpec
from engine.risk.state_machine import RiskStateMachine


pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)


def seed_pending_proposal(session: Session, *, child_code: str | None = None):
    strategy = session.get(StrategyRegistry, ("EMA-CROSS-001", "1.0.0"))
    assert strategy is not None
    broker = BrokerAccount(
        broker_account_id=uuid4(), account_key=f"gen-{uuid4()}", broker_name="TEST",
        environment="PAPER", status="ACTIVE", effective_from=NOW,
    )
    instrument = Instrument(
        instrument_id=uuid4(), symbol=f"GEN{uuid4().hex[:8]}", asset_class="EQUITY",
        currency="USD", effective_from=NOW,
    )
    session.add_all([broker, instrument])
    session.flush()
    parent = CapitalCell(
        cell_id=uuid4(), cell_code=f"P-{uuid4().hex[:8]}", seed_capital=Decimal("100"),
        status="ACTIVE", autonomy_tier="APPRENTICE", strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag, target_treasury_code=instrument.symbol,
        economic_domain="SYNTHETIC",
    )
    session.add(parent)
    session.flush()
    target = CellTreasuryConfig(
        config_id=uuid4(), cell_id=parent.cell_id, target_type="SINGLE_ASSET",
        target_instrument_id=instrument.instrument_id, target_symbol=instrument.symbol,
        config_version=1, is_active=True, authorized_by="TEST", created_at=NOW,
    )
    session.add(target)
    session.flush()
    siphon = SiphonEvent(
        siphon_id=uuid4(), cell_id=parent.cell_id, treasury_code=instrument.symbol,
        amount=Decimal("100"), occurred_at=NOW, reason_code="TEST",
        policy_id="PROFIT-ALLOC-v1.0", policy_version="1.0.0",
        broker_account_id=None, settlement_snapshot_id=None, source_fill_ids=[],
        qualified_profit_usd=Decimal("100"), safety_reserve_usd=Decimal("0"),
        target_treasury_usd=Decimal("0"), replication_pool_usd=Decimal("100"),
        target_config_id=target.config_id, is_synthetic=True,
        synthetic_settlement_metadata={
            "settlement_evidence_type": "SYNTHETIC_REPLAY_SETTLEMENT",
            "synthetic_settled_at": NOW.isoformat(), "replay_session_id": "STEP3B",
            "model_version": "SETTLEMENT-SIM-v0.1",
        },
        source_manifest_hash="b" * 64,
    )
    session.add(siphon)
    session.flush()
    allocation = SiphonAllocation(
        allocation_id=uuid4(), siphon_id=siphon.siphon_id,
        bucket_type="REPLICATION_POOL", allocated_usd=Decimal("100"),
        unallocated_cash_balance_usd=Decimal("0"), occurred_at=NOW,
    )
    session.add(allocation)
    session.flush()
    proposal = ReplicationManager(session).create_proposal(
        parent_cell_id=parent.cell_id,
        proposed_child_code=child_code or f"C-{uuid4().hex[:8]}",
        is_synthetic=True,
        occurred_at=NOW,
    )
    assert proposal is not None
    return parent, target, instrument, allocation, proposal


def approve(session: Session, proposal, *, at: datetime = NOW + timedelta(seconds=1)):
    return HumanAuthorizationService(session).authorize(
        proposal_id=proposal.proposal_id,
        manifest_hash=proposal.manifest_hash,
        decision=AuthorizationDecision.APPROVE,
        authorized_by="HUMAN-OPERATOR",
        authorization_method="SIGNED_REVIEW",
        authorized_at=at,
    )


def instantiate(session: Session, proposal, *, at: datetime = NOW + timedelta(seconds=2)):
    approve(session, proposal)
    return GenesisFactory(session).instantiate_child_cell(
        proposal_id=proposal.proposal_id, occurred_at=at
    )


def test_genesis_capital_authorization_uses_canonical_cash_fields(db_session: Session) -> None:
    *_, proposal = seed_pending_proposal(db_session)
    child = instantiate(db_session, proposal)
    row = db_session.scalar(select(KairoCapitalAuthorizationRecord).where(
        KairoCapitalAuthorizationRecord.cell_id == child.cell_id
    ))
    assert row.economic_domain == "SYNTHETIC"
    assert row.broker_account_id is None and row.broker_snapshot_id is None
    assert row.settled_cash == row.authorized_trading_cash == Decimal("100.00")
    assert row.safety_reserve == row.ownership_treasury_reserved == Decimal("0.00")
    assert row.replication_reserve == row.committed_obligations == Decimal("0.00")


def test_genesis_manifest_contains_complete_versioned_evidence_identity(db_session: Session) -> None:
    *_, proposal = seed_pending_proposal(db_session)
    child = instantiate(db_session, proposal)
    row = db_session.scalar(select(SyntheticEvidenceManifest).where(
        SyntheticEvidenceManifest.cell_id == child.cell_id
    ))
    assert (row.manifest_type, row.manifest_algorithm) == (
        "GENESIS_SEED", "GENESIS-SEED-MANIFEST-v1"
    )
    assert (row.model_identifier, row.model_version) == ("KAIRO-GENESIS", "1.0.0")
    assert row.source_count == 3
    authorization = db_session.scalar(select(ReplicationAuthorization).where(
        ReplicationAuthorization.proposal_id == proposal.proposal_id
    ))
    reservation = db_session.scalar(select(ReplicationProposalReservation).where(
        ReplicationProposalReservation.proposal_id == proposal.proposal_id
    ))
    rebuilt = GenesisSeedManifest(
        proposal_id=proposal.proposal_id,
        proposal_manifest_hash=proposal.manifest_hash,
        authorization_id=authorization.authorization_id,
        parent_cell_id=proposal.parent_cell_id,
        child_cell_id=child.cell_id,
        child_cell_code=child.cell_code,
        seed_capital_usd=Decimal("100.00"),
        strategy_identifier=proposal.strategy_identifier,
        strategy_version=proposal.strategy_version,
        risk_policy_identifier=proposal.risk_policy_identifier,
        target_config_id=proposal.target_config_id,
        target_type=proposal.target_type,
        target_instrument_id=proposal.target_instrument_id,
        target_symbol=proposal.target_symbol,
        target_treasury_code=proposal.target_treasury_code,
        created_at=NOW + timedelta(seconds=2),
        source_allocations=(GenesisAllocationSource(
            allocation_id=reservation.allocation_id,
            reservation_id=reservation.reservation_id,
            reserved_usd=reservation.reserved_usd,
        ),),
    )
    assert row.manifest_hash.strip() == rebuilt.sha256()


def test_consumption_requires_matching_reserved_proposal_allocation(db_session: Session) -> None:
    parent, target, _, _, proposal = seed_pending_proposal(db_session)
    approve(db_session, proposal)
    child = CapitalCell(
        cell_id=uuid4(), cell_code=proposal.proposed_child_code, seed_capital=Decimal("100"),
        status="INITIALIZING", autonomy_tier="APPRENTICE",
        strategy_id=proposal.strategy_identifier, strategy_version=proposal.strategy_version,
        target_treasury_code=proposal.target_treasury_code,
        risk_policy_id=parent.risk_policy_id, economic_domain="SYNTHETIC",
    )
    db_session.add(child)
    db_session.flush()
    other = SiphonAllocation(
        allocation_id=uuid4(), siphon_id=db_session.scalar(select(SiphonEvent.siphon_id)),
        bucket_type="TARGET_TREASURY", allocated_usd=Decimal("1"),
        unallocated_cash_balance_usd=Decimal("0"), occurred_at=NOW,
    )
    db_session.add(other)
    db_session.flush()
    with pytest.raises(DBAPIError, match="No reservation exists"), db_session.begin_nested():
        db_session.add(ReplicationCashConsumption(
            consumption_id=uuid4(), proposal_id=proposal.proposal_id,
            child_cell_id=child.cell_id, allocation_id=other.allocation_id,
            consumed_usd=Decimal("1"), is_synthetic=True, occurred_at=NOW + timedelta(seconds=2),
        ))
        db_session.flush()


def test_consumption_cannot_exceed_reserved_amount(db_session: Session) -> None:
    parent, _, _, allocation, proposal = seed_pending_proposal(db_session)
    approve(db_session, proposal)
    child = CapitalCell(
        cell_id=uuid4(), cell_code=proposal.proposed_child_code, seed_capital=Decimal("100"),
        status="INITIALIZING", autonomy_tier="APPRENTICE", strategy_id=proposal.strategy_identifier,
        strategy_version=proposal.strategy_version, target_treasury_code=proposal.target_treasury_code,
        risk_policy_id=parent.risk_policy_id, economic_domain="SYNTHETIC",
    )
    db_session.add(child)
    db_session.flush()
    with pytest.raises(DBAPIError, match="exceeds reserved amount"), db_session.begin_nested():
        db_session.add(ReplicationCashConsumption(
            consumption_id=uuid4(), proposal_id=proposal.proposal_id,
            child_cell_id=child.cell_id, allocation_id=allocation.allocation_id,
            consumed_usd=Decimal("100.01"), is_synthetic=True,
            occurred_at=NOW + timedelta(seconds=2),
        ))
        db_session.flush()


def test_consumption_cannot_use_released_or_consumed_reservation(db_session: Session) -> None:
    parent, _, _, allocation, proposal = seed_pending_proposal(db_session)
    approve(db_session, proposal)
    child = CapitalCell(
        cell_id=uuid4(), cell_code=proposal.proposed_child_code, seed_capital=Decimal("100"),
        status="INITIALIZING", autonomy_tier="APPRENTICE", strategy_id=proposal.strategy_identifier,
        strategy_version=proposal.strategy_version, target_treasury_code=proposal.target_treasury_code,
        risk_policy_id=parent.risk_policy_id, economic_domain="SYNTHETIC",
    )
    db_session.add(child)
    db_session.flush()
    reservation = db_session.scalar(select(ReplicationProposalReservation).where(
        ReplicationProposalReservation.proposal_id == proposal.proposal_id
    ))
    db_session.add(ReplicationReservationEvent(
        event_id=uuid4(), reservation_id=reservation.reservation_id,
        event_type="RELEASED", reason_code="TEST", occurred_at=NOW + timedelta(seconds=2),
    ))
    db_session.flush()
    with pytest.raises(DBAPIError, match="expected RESERVED"), db_session.begin_nested():
        db_session.add(ReplicationCashConsumption(
            consumption_id=uuid4(), proposal_id=proposal.proposal_id,
            child_cell_id=child.cell_id, allocation_id=allocation.allocation_id,
            consumed_usd=Decimal("100"), is_synthetic=True,
            occurred_at=NOW + timedelta(seconds=3),
        ))
        db_session.flush()


def test_proposal_transition_matrix_rejects_every_unlisted_transition(db_session: Session) -> None:
    *_, proposal = seed_pending_proposal(db_session)
    for target in ("PENDING_AUTHORIZATION", "EXECUTED"):
        with pytest.raises(DBAPIError, match="Illegal transition"), db_session.begin_nested():
            db_session.add(ReplicationProposalEvent(
                event_id=uuid4(), proposal_id=proposal.proposal_id,
                state_from="PENDING_AUTHORIZATION", state_to=target,
                reason_code="TEST", occurred_at=NOW + timedelta(seconds=1),
            ))
            db_session.flush()
    approve(db_session, proposal)
    for target in ("REJECTED", "CANCELLED", "EXPIRED", "AUTHORIZED"):
        with pytest.raises(DBAPIError, match="Illegal transition"), db_session.begin_nested():
            db_session.add(ReplicationProposalEvent(
                event_id=uuid4(), proposal_id=proposal.proposal_id,
                state_from="AUTHORIZED", state_to=target,
                reason_code="TEST", occurred_at=NOW + timedelta(seconds=2),
            ))
            db_session.flush()


def test_executed_transition_requires_child_manifest_capital_authorization_and_treasury_config(
    db_session: Session,
) -> None:
    *_, proposal = seed_pending_proposal(db_session)
    approve(db_session, proposal)
    with pytest.raises(DBAPIError, match="genesis_events"), db_session.begin_nested():
        db_session.add(ReplicationProposalEvent(
            event_id=uuid4(), proposal_id=proposal.proposal_id,
            state_from="AUTHORIZED", state_to="EXECUTED", reason_code="TEST",
            occurred_at=NOW + timedelta(seconds=2),
        ))
        db_session.flush()


def test_genesis_target_treasury_code_is_bound_before_human_approval(db_session: Session) -> None:
    _, target, _, _, proposal = seed_pending_proposal(db_session)
    assert proposal.target_config_id == target.config_id
    assert proposal.target_instrument_id == target.target_instrument_id
    assert proposal.target_symbol == proposal.target_treasury_code == target.target_symbol


def test_genesis_does_not_read_changed_parent_treasury_identity_after_approval(
    db_session: Session,
) -> None:
    parent, target, original, _, proposal = seed_pending_proposal(db_session)
    approve(db_session, proposal)
    replacement = Instrument(
        instrument_id=uuid4(), symbol=f"NEW{uuid4().hex[:8]}", asset_class="EQUITY",
        currency="USD", effective_from=NOW,
    )
    db_session.add(replacement)
    db_session.flush()
    target.target_instrument_id = replacement.instrument_id
    target.target_symbol = replacement.symbol
    parent.target_treasury_code = replacement.symbol
    db_session.flush()
    child = GenesisFactory(db_session).instantiate_child_cell(
        proposal_id=proposal.proposal_id, occurred_at=NOW + timedelta(seconds=2)
    )
    config = db_session.scalar(select(CellTreasuryConfig).where(
        CellTreasuryConfig.cell_id == child.cell_id
    ))
    assert child.target_treasury_code == original.symbol
    assert config.target_instrument_id == original.instrument_id
    assert config.target_symbol == original.symbol


def test_genesis_does_not_create_fake_risk_session(db_session: Session) -> None:
    *_, proposal = seed_pending_proposal(db_session)
    child = instantiate(db_session, proposal)
    assert db_session.scalar(select(RiskSession).where(RiskSession.cell_id == child.cell_id)) is None
    assert db_session.get(RiskGovernorState, child.cell_id) is None


def test_first_real_child_session_initializes_disarmed_zero_pnl_state(db_session: Session) -> None:
    *_, proposal = seed_pending_proposal(db_session)
    child = instantiate(db_session, proposal)
    state = RiskStateMachine(db_session, cell_id=child.cell_id).initialize_session(
        RiskSessionSpec(
            session_id=f"CHILD-{uuid4().hex[:8]}", trading_date=NOW.date(),
            session_open=NOW + timedelta(hours=1), session_close=NOW + timedelta(hours=7),
        )
    )
    assert state.operational_state == "DISARMED"
    assert state.session_realized_pnl == state.session_unrealized_pnl == Decimal("0")
    assert state.session_fees_usd == state.session_slippage_usd == state.session_net_pnl == Decimal("0")


def test_runtime_cannot_insert_human_replication_authorization(
    migrated_database: tuple[str, str],
) -> None:
    _, runtime_url = migrated_database
    runtime = create_engine(runtime_url)
    with pytest.raises(DBAPIError, match="permission denied"):
        with runtime.begin() as connection:
            connection.execute(text(
                "INSERT INTO replication_authorizations "
                "(authorization_id, proposal_id, manifest_hash, decision, authorized_by, "
                "authorization_method, authorized_at) VALUES "
                "(:a, :p, :h, 'APPROVE', 'TEST', 'TEST', :t)"
            ), {"a": uuid4(), "p": uuid4(), "h": "c" * 64, "t": NOW})
    runtime.dispose()


def test_genesis_failure_rolls_back_entire_transaction_cleanly(db_session: Session) -> None:
    *_, proposal = seed_pending_proposal(db_session)
    approve(db_session, proposal)

    def fail_on_consumption(session, flush_context, instances):
        if any(isinstance(row, ReplicationCashConsumption) for row in session.new):
            raise RuntimeError("injected genesis failure")

    event.listen(db_session, "before_flush", fail_on_consumption)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            GenesisFactory(db_session).instantiate_child_cell(
                proposal_id=proposal.proposal_id, occurred_at=NOW + timedelta(seconds=2)
            )
    finally:
        event.remove(db_session, "before_flush", fail_on_consumption)
    assert db_session.scalar(select(CapitalCell).where(
        CapitalCell.cell_code == proposal.proposed_child_code
    )) is None
    assert db_session.scalar(select(CellGenesisEvent).where(
        CellGenesisEvent.proposal_id == proposal.proposal_id
    )) is None
    assert db_session.scalar(select(ReplicationCashConsumption).where(
        ReplicationCashConsumption.proposal_id == proposal.proposal_id
    )) is None


def test_genesis_is_idempotent_and_cannot_run_twice_for_same_proposal(db_session: Session) -> None:
    *_, proposal = seed_pending_proposal(db_session)
    child = instantiate(db_session, proposal)
    again = GenesisFactory(db_session).instantiate_child_cell(
        proposal_id=proposal.proposal_id, occurred_at=NOW + timedelta(seconds=3)
    )
    assert again.cell_id == child.cell_id
    assert db_session.scalar(select(func.count()).select_from(CellGenesisEvent).where(
        CellGenesisEvent.proposal_id == proposal.proposal_id
    )) == 1
    assert db_session.scalar(select(func.count()).select_from(ReplicationCashConsumption).where(
        ReplicationCashConsumption.proposal_id == proposal.proposal_id
    )) == 1


def test_migration_0015_upgrade_and_downgrade_are_clean_and_data_safe(
    migrated_database: tuple[str, str],
) -> None:
    admin_url, _ = migrated_database
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    engine = create_engine(admin_url)
    command.downgrade(config, "0014")
    assert "replication_authorizations" not in inspect(engine).get_table_names()
    command.upgrade(config, "0015")
    assert {
        "replication_authorizations", "replication_cash_consumptions", "cell_genesis_events"
    } <= set(inspect(engine).get_table_names())
    command.downgrade(config, "0014")
    command.upgrade(config, "head")
    engine.dispose()
