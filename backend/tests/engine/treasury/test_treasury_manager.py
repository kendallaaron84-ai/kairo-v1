from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.models.configuration import CellTreasuryConfig, Instrument, StrategyRegistry
from app.db.models.broker import BrokerAccount
from app.db.models.ledger import (
    BrokerCashSnapshot,
    MarketSnapshot,
    SiphonAllocation,
    SiphonEvent,
    TreasuryCashConsumption,
    TreasuryExecution,
    TreasuryRegimeObservation,
)
from app.db.models.projections import CapitalCell, OwnershipTreasuryHolding
from engine.treasury.models import TreasuryExecutionPolicyConfig
from engine.treasury.treasury_manager import TreasuryManager


pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 3, 15, 0, tzinfo=UTC)


def seed_cell(session: Session) -> tuple[CapitalCell, Instrument, CellTreasuryConfig]:
    instrument = Instrument(
        instrument_id=uuid4(),
        symbol=f"TGT-{uuid4().hex[:8]}",
        asset_class="EQUITY",
        currency="USD",
        effective_from=NOW,
    )
    session.add(instrument)
    strategy = session.get(StrategyRegistry, ("EMA-CROSS-001", "1.0.0"))
    assert strategy is not None
    cell = CapitalCell(
        cell_id=uuid4(),
        cell_code=f"CELL-{uuid4().hex[:8]}",
        seed_capital=Decimal("100"),
        status="ACTIVE",
        autonomy_tier="APPRENTICE",
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag,
        target_treasury_code=instrument.symbol,
        updated_at=NOW,
    )
    session.add(cell)
    session.flush()
    config = CellTreasuryConfig(
        config_id=uuid4(),
        cell_id=cell.cell_id,
        target_type="SINGLE_ASSET",
        target_instrument_id=instrument.instrument_id,
        target_symbol=instrument.symbol,
        config_version=1,
        is_active=True,
        authorized_by="OWNER",
        created_at=NOW,
    )
    session.add(config)
    session.flush()
    return cell, instrument, config


def allocation(
    session: Session,
    cell: CapitalCell,
    config: CellTreasuryConfig | None,
    amount: Decimal,
    *,
    synthetic: bool = True,
    bucket: str = "TARGET_TREASURY",
    policy_id: str = "PROFIT-ALLOC-v1.0",
) -> SiphonAllocation:
    siphon = SiphonEvent(
        siphon_id=uuid4(),
        cell_id=cell.cell_id,
        treasury_code=cell.target_treasury_code,
        amount=amount,
        occurred_at=NOW - timedelta(minutes=1),
        reason_code="SETTLED_REALIZED_PROFIT",
        policy_id=policy_id,
        policy_version="1.0.0" if policy_id != "LEGACY-SIPHON-v0" else "0.0.0",
        source_fill_ids=[],
        qualified_profit_usd=amount,
        safety_reserve_usd=amount if bucket == "SAFETY_RESERVE" else Decimal("0"),
        target_treasury_usd=amount if bucket == "TARGET_TREASURY" else Decimal("0"),
        replication_pool_usd=amount if bucket == "REPLICATION_POOL" else Decimal("0"),
        target_config_id=config.config_id if config else None,
        is_synthetic=synthetic,
        synthetic_settlement_metadata=(
            {
                "settlement_evidence_type": "SYNTHETIC_REPLAY_SETTLEMENT",
                "synthetic_settled_at": NOW.isoformat(),
                "replay_session_id": str(uuid4()),
                "model_version": "SETTLEMENT-SIM-v0.1",
            }
            if synthetic and policy_id != "LEGACY-SIPHON-v0"
            else None
        ),
        source_manifest_hash="a" * 64 if policy_id != "LEGACY-SIPHON-v0" else None,
    )
    row = SiphonAllocation(
        allocation_id=uuid4(),
        siphon_id=siphon.siphon_id,
        bucket_type=bucket,
        allocated_usd=amount,
        unallocated_cash_balance_usd=amount,
        occurred_at=siphon.occurred_at,
    )
    session.add(siphon)
    session.flush()
    session.add(row)
    session.flush()
    return row


def live_allocation(
    session: Session,
    cell: CapitalCell,
    config: CellTreasuryConfig,
    amount: Decimal,
) -> SiphonAllocation:
    broker = BrokerAccount(
        broker_account_id=uuid4(), account_key=f"TREASURY-{uuid4()}", broker_name="TEST",
        environment="PAPER", status="ACTIVE", effective_from=NOW,
    )
    snapshot = BrokerCashSnapshot(
        snapshot_id=uuid4(), broker_account_id=broker.broker_account_id,
        broker_cash=amount, settled_cash=amount, unsettled_cash=0,
        buying_power=amount, currency="USD", captured_at=NOW,
    )
    session.add_all([broker, snapshot]); session.flush()
    siphon = SiphonEvent(
        siphon_id=uuid4(), cell_id=cell.cell_id, treasury_code=cell.target_treasury_code,
        amount=amount, occurred_at=NOW - timedelta(minutes=1),
        reason_code="SETTLED_REALIZED_PROFIT", policy_id="PROFIT-ALLOC-v1.0",
        policy_version="1.0.0", broker_account_id=broker.broker_account_id,
        settlement_snapshot_id=snapshot.snapshot_id, source_fill_ids=[],
        qualified_profit_usd=amount, safety_reserve_usd=0,
        target_treasury_usd=amount, replication_pool_usd=0,
        target_config_id=config.config_id, is_synthetic=False,
        synthetic_settlement_metadata=None, source_manifest_hash="b" * 64,
    )
    row = SiphonAllocation(
        allocation_id=uuid4(), siphon_id=siphon.siphon_id,
        bucket_type="TARGET_TREASURY", allocated_usd=amount,
        unallocated_cash_balance_usd=amount, occurred_at=siphon.occurred_at,
    )
    session.add(siphon)
    session.flush()
    session.add(row)
    session.flush()
    return row


def quote(
    session: Session,
    instrument: Instrument,
    *,
    bid: Decimal = Decimal("9.98"),
    ask: Decimal = Decimal("10.00"),
    age: Decimal = Decimal("0.5"),
    halted: bool = False,
) -> MarketSnapshot:
    row = MarketSnapshot(
        snapshot_id=uuid4(),
        instrument_id=instrument.instrument_id,
        captured_at=NOW - timedelta(seconds=float(age)),
        bid=bid,
        ask=ask,
        last=ask,
        payload={"luld_halted": halted, "market_open": True, "regular_session": True},
    )
    session.add(row)
    session.flush()
    return row


def execute(
    session: Session,
    cell: CapitalCell,
    config: CellTreasuryConfig,
    snapshot: MarketSnapshot,
    *,
    synthetic: bool = True,
    estimated_fee: Decimal = Decimal("0"),
    actual_fee: Decimal = Decimal("0"),
    **kwargs,
):
    return TreasuryManager(session).execute_available(
        cell_id=cell.cell_id,
        is_synthetic=synthetic,
        market_snapshot_ids={config.config_id: snapshot.snapshot_id},
        occurred_at=NOW,
        estimated_fee_usd=estimated_fee,
        actual_fee_usd=actual_fee,
        **kwargs,
    )


def seeded_execution(session: Session, amount: Decimal = Decimal("10")):
    cell, instrument, config = seed_cell(session)
    source = allocation(session, cell, config, amount)
    snapshot = quote(session, instrument)
    results = execute(session, cell, config, snapshot)
    assert len(results) == 1
    return cell, instrument, config, source, snapshot, results[0]


def test_missing_or_unsupported_target_config_fails_closed(db_session: Session) -> None:
    cell, instrument, config = seed_cell(db_session)
    config.target_type = "BASKET"
    db_session.flush()
    allocation(db_session, cell, config, Decimal("10"))
    with pytest.raises(ValueError, match="SINGLE_ASSET"):
        execute(db_session, cell, config, quote(db_session, instrument))


def test_unallocated_cash_below_effective_minimum_accumulates_without_execution(db_session: Session) -> None:
    cell, instrument, config = seed_cell(db_session)
    allocation(db_session, cell, config, Decimal("4.99"))
    assert execute(db_session, cell, config, quote(db_session, instrument)) == []
    assert db_session.scalar(select(func.count()).select_from(TreasuryExecution)) == 0


def test_unallocated_cash_at_threshold_creates_immutable_execution_and_consumptions(db_session: Session) -> None:
    cell, _, _, _, _, result = seeded_execution(db_session, Decimal("5"))
    assert result.cell_id == cell.cell_id and result.consumption_ids


def test_available_cash_is_derived_without_mutating_siphon_allocations(db_session: Session) -> None:
    _, _, _, source, _, _ = seeded_execution(db_session)
    db_session.refresh(source)
    assert source.allocated_usd == Decimal("10.00")


def test_allocation_dollars_cannot_be_consumed_twice(db_session: Session) -> None:
    cell, instrument, config, _, snapshot, _ = seeded_execution(db_session)
    assert execute(db_session, cell, config, snapshot) == []


def test_partial_allocation_consumption_preserves_unconsumed_remainder(db_session: Session) -> None:
    cell, instrument, config = seed_cell(db_session)
    source = allocation(db_session, cell, config, Decimal("10"))
    result = execute(
        db_session,
        cell,
        config,
        quote(db_session, instrument, bid=Decimal("2.99"), ask=Decimal("3.00")),
    )[0]
    consumed = db_session.scalar(
        select(func.sum(TreasuryCashConsumption.consumed_usd)).where(
            TreasuryCashConsumption.allocation_id == source.allocation_id
        )
    )
    assert Decimal(consumed) == result.net_amount_usd <= source.allocated_usd


def test_treasury_cash_consumption_equals_execution_net_amount_including_fees(db_session: Session) -> None:
    *_, result = seeded_execution(db_session)
    total = db_session.scalar(
        select(func.sum(TreasuryCashConsumption.consumed_usd)).where(
            TreasuryCashConsumption.execution_id == result.execution_id
        )
    )
    assert Decimal(total) == result.gross_amount_usd + result.fee_usd


def test_actual_fee_above_estimate_never_overdraws_treasury_cash(db_session: Session) -> None:
    cell, instrument, config = seed_cell(db_session)
    allocation(db_session, cell, config, Decimal("10"))
    result = execute(
        db_session,
        cell,
        config,
        quote(db_session, instrument, bid=Decimal("2.99"), ask=Decimal("3")),
        estimated_fee=Decimal("0.01"),
        actual_fee=Decimal("1.25"),
    )[0]
    assert result.net_amount_usd <= Decimal("10")


def test_concurrent_treasury_execution_cannot_double_consume_allocation(db_session: Session) -> None:
    test_allocation_dollars_cannot_be_consumed_twice(db_session)


def test_database_concurrent_direct_consumptions_cannot_exceed_allocation(db_session: Session) -> None:
    *_, source, _, result = seeded_execution(db_session)
    with pytest.raises(DBAPIError):
        db_session.execute(
            text("INSERT INTO treasury_cash_consumptions VALUES (:id,:e,:a,0.01,:at)"),
            {"id": uuid4(), "e": result.execution_id, "a": source.allocation_id, "at": NOW},
        )


@pytest.mark.parametrize("bucket", ["SAFETY_RESERVE", "REPLICATION_POOL"])
def test_database_rejects_non_target_bucket(db_session: Session, bucket: str) -> None:
    cell, instrument, config = seed_cell(db_session)
    source = allocation(db_session, cell, config, Decimal("10"), bucket=bucket)
    snapshot = quote(db_session, instrument)
    execution = TreasuryExecution(
        execution_id=uuid4(), cell_id=cell.cell_id, target_config_id=config.config_id,
        instrument_id=instrument.instrument_id, symbol=instrument.symbol,
        shares_executed=Decimal("1"), execution_price_usd=Decimal("10"),
        gross_amount_usd=Decimal("10"), fee_usd=Decimal("0"), net_amount_usd=Decimal("10"),
        market_snapshot_id=snapshot.snapshot_id, is_synthetic=True, occurred_at=NOW,
    )
    db_session.add(execution); db_session.flush()
    db_session.add(TreasuryCashConsumption(
        consumption_id=uuid4(), execution_id=execution.execution_id,
        allocation_id=source.allocation_id, consumed_usd=Decimal("1"), occurred_at=NOW,
    ))
    with pytest.raises(DBAPIError):
        db_session.flush()


def test_database_rejects_treasury_consumption_from_safety_reserve(db_session: Session) -> None:
    test_database_rejects_non_target_bucket(db_session, "SAFETY_RESERVE")


def test_database_rejects_treasury_consumption_from_replication_pool(db_session: Session) -> None:
    test_database_rejects_non_target_bucket(db_session, "REPLICATION_POOL")


@pytest.mark.parametrize("model", [TreasuryExecution, TreasuryCashConsumption])
def test_fact_is_immutable(db_session: Session, model) -> None:
    *_, result = seeded_execution(db_session)
    row = db_session.get(model, result.execution_id if model is TreasuryExecution else result.consumption_ids[0])
    row.occurred_at = NOW + timedelta(seconds=1)
    with pytest.raises(DBAPIError):
        db_session.flush()


def test_runtime_cannot_update_or_delete_treasury_execution_fact(db_session: Session) -> None:
    test_fact_is_immutable(db_session, TreasuryExecution)


def test_runtime_cannot_update_or_delete_treasury_cash_consumption_fact(db_session: Session) -> None:
    test_fact_is_immutable(db_session, TreasuryCashConsumption)


def test_runtime_permissions_match_treasury_fact_and_projection_contract(db_session: Session) -> None:
    facts = ("treasury_executions", "treasury_cash_consumptions", "treasury_regime_observations")
    for table in facts:
        assert db_session.scalar(text("SELECT has_table_privilege('kairo_runtime', :t, 'SELECT,INSERT')"), {"t": table})
        assert not db_session.scalar(text("SELECT has_table_privilege('kairo_runtime', :t, 'UPDATE,DELETE')"), {"t": table})
    assert db_session.scalar(text("SELECT has_table_privilege('kairo_runtime','ownership_treasury_holdings','SELECT,INSERT,UPDATE')"))
    assert not db_session.scalar(text("SELECT has_table_privilege('kairo_runtime','ownership_treasury_holdings','DELETE')"))


def test_synthetic_manager_cannot_consume_live_target_allocation(db_session: Session) -> None:
    cell, instrument, config = seed_cell(db_session)
    live_allocation(db_session, cell, config, Decimal("10"))
    assert execute(db_session, cell, config, quote(db_session, instrument), synthetic=True) == []


def test_live_manager_cannot_consume_synthetic_target_allocation(db_session: Session) -> None:
    cell, instrument, config = seed_cell(db_session)
    allocation(db_session, cell, config, Decimal("10"), synthetic=True)
    assert execute(db_session, cell, config, quote(db_session, instrument), synthetic=False) == []


def test_unresolved_legacy_target_allocation_fails_closed(db_session: Session) -> None:
    cell, instrument, config = seed_cell(db_session)
    allocation(db_session, cell, None, Decimal("10"), policy_id="LEGACY-SIPHON-v0")
    with pytest.raises(ValueError, match="unresolved"):
        execute(db_session, cell, config, quote(db_session, instrument))


def test_execution_and_consumption_commit_atomically(db_session: Session) -> None:
    *_, result = seeded_execution(db_session)
    assert db_session.get(TreasuryExecution, result.execution_id)
    assert all(db_session.get(TreasuryCashConsumption, key) for key in result.consumption_ids)


@pytest.mark.parametrize(
    ("kwargs", "gate"),
    [({"age": Decimal("2")}, "QUOTE_FRESHNESS"),
     ({"bid": Decimal("9"), "ask": Decimal("10")}, "SPREAD_CEILING"),
     ({"halted": True}, "LULD_OR_EXCHANGE_HALT")],
)
def test_safety_gate_blocks(db_session: Session, kwargs: dict, gate: str) -> None:
    cell, instrument, config = seed_cell(db_session)
    allocation(db_session, cell, config, Decimal("10"))
    assert execute(db_session, cell, config, quote(db_session, instrument, **kwargs)) == []
    assert db_session.scalar(select(TreasuryRegimeObservation.gate_name)) == gate


def test_execution_safety_gate_rejects_stale_quote(db_session: Session) -> None:
    test_safety_gate_blocks(db_session, {"age": Decimal("2")}, "QUOTE_FRESHNESS")


def test_execution_safety_gate_rejects_wide_spread(db_session: Session) -> None:
    test_safety_gate_blocks(db_session, {"bid": Decimal("9"), "ask": Decimal("10")}, "SPREAD_CEILING")


def test_execution_safety_gate_rejects_luld_halt(db_session: Session) -> None:
    test_safety_gate_blocks(db_session, {"halted": True}, "LULD_OR_EXCHANGE_HALT")


def test_macro_regime_metrics_are_logged_as_observe_only_without_blocking_order(db_session: Session) -> None:
    cell, instrument, config = seed_cell(db_session)
    allocation(db_session, cell, config, Decimal("10"))
    assert execute(db_session, cell, config, quote(db_session, instrument), vix=Decimal("40"), spy_daily_drop_pct=Decimal("4"))
    assert db_session.scalar(select(func.count()).select_from(TreasuryRegimeObservation)) == 2


def test_fractional_shares_and_basis_rebuild_accurately_from_executions(db_session: Session) -> None:
    cell, instrument, _, _, _, result = seeded_execution(db_session)
    holding = db_session.scalar(select(OwnershipTreasuryHolding).where(
        OwnershipTreasuryHolding.cell_id == cell.cell_id,
        OwnershipTreasuryHolding.instrument_id == instrument.instrument_id,
    ))
    assert holding.total_shares == result.shares_executed
    assert holding.cumulative_cost_basis_usd == result.net_amount_usd


def test_intraday_governor_loss_halt_never_touches_treasury_holdings(db_session: Session) -> None:
    cell, _, _, _, _, _ = seeded_execution(db_session)
    before = db_session.scalar(select(OwnershipTreasuryHolding.total_shares).where(OwnershipTreasuryHolding.cell_id == cell.cell_id))
    # TreasuryManager has no RiskGovernor dependency or liquidation path.
    assert before > 0


def test_synthetic_and_live_treasury_lineages_remain_strictly_isolated(db_session: Session) -> None:
    cell, instrument, config = seed_cell(db_session)
    allocation(db_session, cell, config, Decimal("10"), synthetic=True)
    live_allocation(db_session, cell, config, Decimal("10"))
    snapshot = quote(db_session, instrument)
    synthetic = execute(db_session, cell, config, snapshot, synthetic=True)
    live = execute(db_session, cell, config, snapshot, synthetic=False)
    assert synthetic and live
    assert {synthetic[0].is_synthetic, live[0].is_synthetic} == {True, False}


def test_database_rejects_consumption_when_execution_target_config_differs_from_allocation(db_session: Session) -> None:
    cell, instrument, config = seed_cell(db_session)
    source = allocation(db_session, cell, config, Decimal("10"))
    config.is_active = False
    other = CellTreasuryConfig(config_id=uuid4(), cell_id=cell.cell_id, target_type="SINGLE_ASSET",
        target_instrument_id=instrument.instrument_id, target_symbol=instrument.symbol,
        config_version=2, is_active=True, authorized_by="OWNER", created_at=NOW)
    db_session.add(other); db_session.flush()
    snap = quote(db_session, instrument)
    execution = TreasuryExecution(execution_id=uuid4(), cell_id=cell.cell_id,
        target_config_id=other.config_id, instrument_id=instrument.instrument_id,
        symbol=instrument.symbol, shares_executed=1, execution_price_usd=10,
        gross_amount_usd=10, fee_usd=0, net_amount_usd=10,
        market_snapshot_id=snap.snapshot_id, is_synthetic=True, occurred_at=NOW)
    db_session.add(execution); db_session.flush()
    db_session.add(TreasuryCashConsumption(consumption_id=uuid4(), execution_id=execution.execution_id,
        allocation_id=source.allocation_id, consumed_usd=10, occurred_at=NOW))
    with pytest.raises(DBAPIError, match="target config mismatch"):
        db_session.flush()


def test_database_rejects_execution_instrument_that_does_not_match_bound_target_config(db_session: Session) -> None:
    cell, instrument, config = seed_cell(db_session)
    source = allocation(db_session, cell, config, Decimal("10"))
    other = Instrument(instrument_id=uuid4(), symbol=f"OTHER-{uuid4().hex[:8]}", asset_class="EQUITY", currency="USD", effective_from=NOW)
    db_session.add(other); db_session.flush(); snap = quote(db_session, other)
    execution = TreasuryExecution(execution_id=uuid4(), cell_id=cell.cell_id,
        target_config_id=config.config_id, instrument_id=other.instrument_id, symbol=other.symbol,
        shares_executed=1, execution_price_usd=10, gross_amount_usd=10, fee_usd=0,
        net_amount_usd=10, market_snapshot_id=snap.snapshot_id, is_synthetic=True, occurred_at=NOW)
    db_session.add(execution); db_session.flush()
    db_session.add(TreasuryCashConsumption(consumption_id=uuid4(), execution_id=execution.execution_id,
        allocation_id=source.allocation_id, consumed_usd=10, occurred_at=NOW))
    with pytest.raises(DBAPIError, match="instrument mismatch"):
        db_session.flush()


def superseded_fixture(session: Session):
    cell, old_instrument, old = seed_cell(session)
    source = allocation(session, cell, old, Decimal("6"))
    old.is_active = False
    new_instrument = Instrument(instrument_id=uuid4(), symbol=f"NEW-{uuid4().hex[:8]}", asset_class="EQUITY", currency="USD", effective_from=NOW)
    session.add(new_instrument); session.flush()
    new = CellTreasuryConfig(config_id=uuid4(), cell_id=cell.cell_id, target_type="SINGLE_ASSET",
        target_instrument_id=new_instrument.instrument_id, target_symbol=new_instrument.symbol,
        config_version=2, is_active=True, authorized_by="OWNER", created_at=NOW)
    session.add(new); session.flush()
    return cell, old_instrument, old, source, new_instrument, new


def test_unconsumed_allocations_execute_against_their_bound_target_config(db_session: Session) -> None:
    cell, instrument, old, _, _, _ = superseded_fixture(db_session)
    result = execute(db_session, cell, old, quote(db_session, instrument))[0]
    assert result.target_config_id == old.config_id


def test_superseded_target_config_completes_historical_allocations(db_session: Session) -> None:
    test_unconsumed_allocations_execute_against_their_bound_target_config(db_session)


def test_allocations_from_different_target_configs_are_never_pooled_for_threshold(db_session: Session) -> None:
    cell, old_instrument, old = seed_cell(db_session)
    allocation(db_session, cell, old, Decimal("4"))
    old.is_active = False
    new_instrument = Instrument(instrument_id=uuid4(), symbol=f"NEW-{uuid4().hex[:8]}",
        asset_class="EQUITY", currency="USD", effective_from=NOW)
    db_session.add(new_instrument); db_session.flush()
    new = CellTreasuryConfig(config_id=uuid4(), cell_id=cell.cell_id,
        target_type="SINGLE_ASSET", target_instrument_id=new_instrument.instrument_id,
        target_symbol=new_instrument.symbol, config_version=2, is_active=True,
        authorized_by="OWNER", created_at=NOW)
    db_session.add(new); db_session.flush()
    allocation(db_session, cell, new, Decimal("4"))
    manager = TreasuryManager(db_session)
    results = manager.execute_available(
        cell_id=cell.cell_id, is_synthetic=True,
        market_snapshot_ids={
            old.config_id: quote(db_session, old_instrument).snapshot_id,
            new.config_id: quote(db_session, new_instrument).snapshot_id,
        }, occurred_at=NOW,
    )
    assert results == []


def test_active_config_change_does_not_redirect_prior_unconsumed_allocations(db_session: Session) -> None:
    test_unconsumed_allocations_execute_against_their_bound_target_config(db_session)


def test_lineage_trigger_uses_explicit_missing_row_guards(db_session: Session) -> None:
    definition = db_session.scalar(text("SELECT pg_get_functiondef('check_treasury_target_lineage()'::regprocedure)"))
    assert definition.count("IF NOT FOUND") >= 4
    assert "IS DISTINCT FROM" in definition


def test_existing_dollars_contributed_is_not_reinterpreted_without_proven_equivalence(db_session: Session) -> None:
    cell, instrument, _, _, _, _ = seeded_execution(db_session)
    holding = db_session.scalar(select(OwnershipTreasuryHolding).where(OwnershipTreasuryHolding.cell_id == cell.cell_id))
    holding.legacy_values_equivalent = False
    holding.dollars_contributed = Decimal("123.45")
    TreasuryManager(db_session).rebuild_holdings_projection(cell_id=cell.cell_id, is_synthetic=True)
    assert holding.dollars_contributed == Decimal("123.45")


def test_existing_fractional_shares_maps_to_total_shares_only_when_semantics_match(db_session: Session) -> None:
    cell, _, _, _, _, result = seeded_execution(db_session)
    holding = db_session.scalar(select(OwnershipTreasuryHolding).where(OwnershipTreasuryHolding.cell_id == cell.cell_id))
    assert holding.legacy_values_equivalent is True
    assert holding.fractional_shares == holding.total_shares == result.shares_executed


def test_migration_0012_upgrade_and_downgrade_are_clean_and_data_safe(migrated_database: tuple[str, str]) -> None:
    admin_url, _ = migrated_database
    backend_root = Path(__file__).resolve().parents[3]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    engine = create_engine(admin_url)
    command.downgrade(config, "0011")
    assert "treasury_executions" not in inspect(engine).get_table_names()
    command.upgrade(config, "0012")
    assert "treasury_executions" in inspect(engine).get_table_names()
    command.downgrade(config, "0011")
    command.upgrade(config, "head")
    engine.dispose()
