from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.db.models.broker import BrokerAccount
from app.db.models.configuration import Instrument, RiskPolicy, StrategyRegistry
from app.db.models.ledger import KairoCapitalAuthorizationRecord, SyntheticEvidenceManifest
from app.db.models.projections import CapitalCell, CurrentPosition
from engine.risk.governor import RiskGovernor
from engine.risk.models import FillAccountingEvent, MarketMark, OperationalState, RiskSessionSpec


pytestmark = pytest.mark.integration
DEFAULT_POLICY_ID = UUID("a0000000-0000-0000-0000-000000000001")


def cell(session: Session, code: str, *, policy_id: UUID = DEFAULT_POLICY_ID) -> CapitalCell:
    strategy = session.get(StrategyRegistry, ("EMA-CROSS-001", "1.0.0"))
    assert strategy is not None
    row = CapitalCell(
        cell_id=uuid4(), cell_code=code, seed_capital=Decimal("1000"),
        status="ACTIVE", autonomy_tier="APPRENTICE", strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag, target_treasury_code="META",
        risk_policy_id=policy_id, economic_domain="SYNTHETIC",
    )
    session.add(row)
    session.flush()
    return row


def manifest(session: Session, owner: CapitalCell) -> SyntheticEvidenceManifest:
    row = SyntheticEvidenceManifest(
        manifest_id=uuid4(), manifest_type="REPLAY_RUN", manifest_hash="a" * 64,
        manifest_algorithm="REPLAY-MANIFEST-v1", cell_id=owner.cell_id,
        source_count=0, source_refs={"financial_ids": []},
        model_identifier="EMA-CROSS-001", model_version="1.0.0",
        created_at=datetime(2026, 8, 31, tzinfo=UTC),
    )
    session.add(row)
    session.flush()
    return row


def authorization(owner: CapitalCell, evidence_id: UUID | None) -> KairoCapitalAuthorizationRecord:
    return KairoCapitalAuthorizationRecord(
        authorization_id=uuid4(), cell_id=owner.cell_id, broker_snapshot_id=None,
        broker_account_id=None, economic_domain="SYNTHETIC",
        synthetic_provenance_id=evidence_id, settled_cash=Decimal("100"),
        safety_reserve=Decimal("0"), ownership_treasury_reserved=Decimal("0"),
        replication_reserve=Decimal("0"), committed_obligations=Decimal("0"),
        authorized_trading_cash=Decimal("100"),
    )


def governor(session: Session, owner: CapitalCell, suffix: str = "0") -> RiskGovernor:
    result = RiskGovernor(session, cell_id=owner.cell_id)
    now = datetime.now(UTC)
    result.initialize_session(RiskSessionSpec(
        session_id=f"{owner.cell_code}-{suffix}-{uuid4()}", trading_date=date.today(),
        session_open=now - timedelta(hours=1), session_close=now + timedelta(hours=6),
    ))
    result.arm(authorized_cash_usd=Decimal("1000"))
    return result


def loss(amount: str) -> FillAccountingEvent:
    return FillAccountingEvent(
        fill_id=uuid4(), kairo_order_id=uuid4(), broker_account_id=uuid4(),
        instrument_id=uuid4(), realized_pnl_delta_usd=Decimal(amount),
        commission_fees_usd=Decimal("0"), slippage_usd=Decimal("0"),
        fill_price=Decimal("1"), filled_qty=Decimal("1"), timestamp=datetime.now(UTC),
    )


def test_synthetic_evidence_manifest_is_append_only(db_session: Session) -> None:
    evidence = manifest(db_session, cell(db_session, "APPEND"))
    with pytest.raises(DBAPIError), db_session.begin_nested():
        db_session.execute(update(SyntheticEvidenceManifest).where(
            SyntheticEvidenceManifest.manifest_id == evidence.manifest_id
        ).values(model_version="2.0.0"))
        db_session.flush()
    with pytest.raises(DBAPIError), db_session.begin_nested():
        db_session.execute(delete(SyntheticEvidenceManifest).where(
            SyntheticEvidenceManifest.manifest_id == evidence.manifest_id
        ))
        db_session.flush()


def test_synthetic_authorization_requires_persisted_manifest_fk(db_session: Session) -> None:
    owner = cell(db_session, "AUTH-FK")
    with pytest.raises(DBAPIError), db_session.begin_nested():
        db_session.add(authorization(owner, uuid4()))
        db_session.flush()


def test_synthetic_capital_authorization_requires_canonical_provenance_fk(db_session: Session) -> None:
    owner = cell(db_session, "AUTH-CANONICAL")
    evidence = manifest(db_session, owner)
    row = authorization(owner, evidence.manifest_id)
    db_session.add(row)
    db_session.flush()
    assert row.synthetic_provenance_id == evidence.manifest_id


def test_live_authorization_rejects_synthetic_manifest(db_session: Session) -> None:
    owner = cell(db_session, "AUTH-LIVE")
    evidence = manifest(db_session, owner)
    row = authorization(owner, evidence.manifest_id)
    row.economic_domain = "LIVE"
    with pytest.raises(DBAPIError), db_session.begin_nested():
        db_session.add(row)
        db_session.flush()


def test_synthetic_authorization_rejects_manifest_from_wrong_cell(db_session: Session) -> None:
    first = cell(db_session, "AUTH-A")
    second = cell(db_session, "AUTH-B")
    evidence = manifest(db_session, first)
    with pytest.raises(DBAPIError), db_session.begin_nested():
        db_session.add(authorization(second, evidence.manifest_id))
        db_session.flush()


def test_capital_authorizations_enforce_mutually_exclusive_live_vs_synthetic_provenance(
    db_session: Session,
) -> None:
    owner = cell(db_session, "AUTH-XOR")
    row = authorization(owner, None)
    with pytest.raises(DBAPIError), db_session.begin_nested():
        db_session.add(row)
        db_session.flush()


def test_governor_loads_and_enforces_cell_linked_risk_policy(db_session: Session) -> None:
    policy = RiskPolicy(
        policy_id=uuid4(), policy_identifier=f"TEST-{uuid4()}",
        daily_loss_floor_usd=Decimal("-1"), daily_profit_lock_usd=Decimal("2"),
        market_stale_seconds=Decimal("3"), created_at=datetime.now(UTC),
    )
    db_session.add(policy)
    db_session.flush()
    owner = cell(db_session, "POLICY", policy_id=policy.policy_id)
    risk = governor(db_session, owner)
    risk.record_fill_accounting(loss("-1"), authorized_cash_usd=Decimal("0"))
    assert risk.current_state().operational_state == OperationalState.HALTED_HARD.value
    assert risk.max_quote_age == timedelta(seconds=3)


def test_risk_governor_state_is_strictly_cell_scoped(db_session: Session) -> None:
    first, second = cell(db_session, "STATE-A"), cell(db_session, "STATE-B")
    one, two = governor(db_session, first), governor(db_session, second)
    assert one.current_state().cell_id == first.cell_id
    assert two.current_state().cell_id == second.cell_id


def test_multiple_cells_maintain_independent_concurrent_risk_states(db_session: Session) -> None:
    first, second = cell(db_session, "CONCURRENT-A"), cell(db_session, "CONCURRENT-B")
    one, two = governor(db_session, first), governor(db_session, second)
    one.halt_trading(authorized_cash_usd=Decimal("100"))
    assert one.current_state().operational_state == OperationalState.MANUAL_PAUSE.value
    assert two.current_state().operational_state == OperationalState.ARMED.value


def test_a001_loss_halt_does_not_change_a002_operational_state(db_session: Session) -> None:
    first, second = cell(db_session, "A001"), cell(db_session, "A002")
    one, two = governor(db_session, first), governor(db_session, second)
    one.record_fill_accounting(loss("-6"), authorized_cash_usd=Decimal("0"))
    assert one.current_state().operational_state == OperationalState.HALTED_HARD.value
    assert two.current_state().operational_state == OperationalState.ARMED.value


def _positions(session: Session) -> tuple[CapitalCell, CapitalCell, Instrument, Instrument]:
    first, second = cell(session, "MARK-A"), cell(session, "MARK-B")
    broker = BrokerAccount(
        broker_account_id=uuid4(), account_key=f"multi-{uuid4()}", broker_name="TEST",
        environment="PAPER", status="ACTIVE",
    )
    left = Instrument(instrument_id=uuid4(), symbol=f"L-{uuid4().hex[:8]}", asset_class="EQUITY", currency="USD")
    right = Instrument(instrument_id=uuid4(), symbol=f"R-{uuid4().hex[:8]}", asset_class="EQUITY", currency="USD")
    session.add_all([broker, left, right])
    session.flush()
    session.add_all([
        CurrentPosition(position_id=uuid4(), cell_id=first.cell_id, broker_account_id=broker.broker_account_id,
                        instrument_id=left.instrument_id, quantity=Decimal("1"), average_price=Decimal("10")),
        CurrentPosition(position_id=uuid4(), cell_id=second.cell_id, broker_account_id=broker.broker_account_id,
                        instrument_id=right.instrument_id, quantity=Decimal("1"), average_price=Decimal("20")),
    ])
    session.flush()
    return first, second, left, right


def test_governor_canonical_positions_are_filtered_by_cell(db_session: Session) -> None:
    first, _, left, _ = _positions(db_session)
    rows = RiskGovernor(db_session, cell_id=first.cell_id)._canonical_open_positions()
    assert [item.instrument_id for item in rows] == [left.instrument_id]


def test_a001_market_mark_does_not_revalue_a002_positions(db_session: Session) -> None:
    first, second, left, _ = _positions(db_session)
    one, two = governor(db_session, first), governor(db_session, second)
    now = datetime.now(UTC)
    one.record_market_mark(
        MarketMark(instrument_id=left.instrument_id, mark_price=Decimal("9"),
                   source_timestamp=now, received_at=now),
        positions=[], authorized_cash_usd=Decimal("1000"),
    )
    assert one.current_state().session_unrealized_pnl == Decimal("-1")
    assert two.current_state().session_unrealized_pnl == Decimal("0")


def test_risk_state_machine_cannot_resolve_state_without_cell_identity(db_session: Session) -> None:
    with pytest.raises(TypeError, match="cell_id"):
        RiskGovernor(db_session)  # type: ignore[call-arg]


def _alembic_config() -> Config:
    root = Path(__file__).resolve().parents[3]
    result = Config(str(root / "alembic.ini"))
    result.set_main_option("script_location", str(root / "alembic"))
    return result


def test_domain_backfill_uses_canonical_fill_order_lineage(migrated_database: tuple[str, str]) -> None:
    admin_url, _ = migrated_database
    config = _alembic_config()
    command.downgrade(config, "0012")
    from sqlalchemy import create_engine
    engine = create_engine(admin_url)
    ids = [str(uuid4()) for _ in range(5)]
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO broker_accounts VALUES (:b,'domain-broker','TEST','PAPER','ACTIVE',now(),NULL)"), {"b": ids[0]})
        conn.execute(text("INSERT INTO instruments (instrument_id,symbol,asset_class,currency,effective_from) VALUES (:i,:s,'EQUITY','USD',now())"), {"i": ids[1], "s": f"DOM-{ids[1][:6]}"})
        conn.execute(text("INSERT INTO capital_cells (cell_id,cell_code,seed_capital,status,autonomy_tier,strategy_id,strategy_version,target_treasury_code,updated_at) VALUES (:c,'A001',100,'ACTIVE','APPRENTICE','EMA-CROSS-001','1.0.0','META',now())"), {"c": ids[2]})
        conn.execute(text("INSERT INTO order_intents (intent_id,cell_id,strategy_id,strategy_version,instrument_id,client_order_key,order_purpose,side,target_quantity,order_type,created_at) VALUES (:x,:c,'EMA-CROSS-001','1.0.0',:i,:k,'ENTRY','BUY',1,'MARKET',now())"), {"x": ids[3], "c": ids[2], "i": ids[1], "k": f"domain-{ids[3]}"})
        conn.execute(text("INSERT INTO kairo_orders (kairo_order_id,intent_id,broker_account_id,status,submitted_at) VALUES (:o,:x,:b,'FILLED',now())"), {"o": ids[4], "x": ids[3], "b": ids[0]})
        conn.execute(text("INSERT INTO fills (fill_id,kairo_order_id,broker_account_id,broker_fill_id,instrument_id,side,quantity,price,commission_fee_usd,is_simulated,simulation_metadata,filled_at) VALUES (uuid_generate_v4(),:o,:b,:f,:i,'BUY',1,10,0,false,'{}',now())"), {"o": ids[4], "b": ids[0], "f": f"domain-{ids[4]}", "i": ids[1]})
    command.upgrade(config, "0013")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT economic_domain FROM capital_cells WHERE cell_id=:c"), {"c": ids[2]}) == "LIVE"
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM fills; DELETE FROM kairo_orders; DELETE FROM order_intents; DELETE FROM capital_cells; DELETE FROM instruments; DELETE FROM broker_accounts"))
    engine.dispose()


def test_cell_with_no_domain_evidence_fails_migration(migrated_database: tuple[str, str]) -> None:
    admin_url, _ = migrated_database
    config = _alembic_config()
    command.downgrade(config, "0012")
    from sqlalchemy import create_engine
    engine = create_engine(admin_url)
    orphan = str(uuid4())
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO capital_cells (cell_id,cell_code,seed_capital,status,autonomy_tier,strategy_id,strategy_version,target_treasury_code,updated_at) VALUES (:c,'ORPHAN',100,'ACTIVE','APPRENTICE','EMA-CROSS-001','1.0.0','META',now())"), {"c": orphan})
    with pytest.raises(Exception, match="no verifiable economic-domain evidence"):
        command.upgrade(config, "0013")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM capital_cells WHERE cell_id=:c"), {"c": orphan})
    command.upgrade(config, "head")
    engine.dispose()


def test_legacy_risk_backfill_requires_unique_a001_proof() -> None:
    migration = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "0013_phase_4_multi_cell_foundation.py"
    source = migration.read_text(encoding="utf-8")
    assert "v_count <> 1 OR v_a001 IS NULL" in source
    assert "LIMIT 1" not in source


def test_migration_0013_upgrade_and_downgrade_are_clean_and_data_safe(
    migrated_database: tuple[str, str],
) -> None:
    admin_url, _ = migrated_database
    config = _alembic_config()
    from sqlalchemy import create_engine
    engine = create_engine(admin_url)
    command.downgrade(config, "0012")
    assert "synthetic_evidence_manifests" not in inspect(engine).get_table_names()
    command.upgrade(config, "0013")
    assert "synthetic_evidence_manifests" in inspect(engine).get_table_names()
    command.downgrade(config, "0012")
    command.upgrade(config, "head")
    engine.dispose()
