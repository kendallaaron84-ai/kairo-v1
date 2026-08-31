from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.db.models.broker import BrokerAccount
from app.db.models.configuration import CellTreasuryConfig, Instrument, StrategyRegistry
from app.db.models.ledger import (
    BrokerCashSnapshot,
    Fill,
    KairoOrder,
    MarketSnapshot,
    OrderIntent,
    SiphonAllocation,
    SiphonEvent,
    SiphonProfitAttribution,
)
from app.db.models.projections import CapitalCell
from app.db.models.risk import RiskGovernorState
from engine.siphon.models import CellTreasuryConfigInput, SyntheticSettlementMetadata, TargetType
from engine.siphon.siphon_manager import SiphonManager


pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 2, 16, 0, tzinfo=UTC)


@dataclass
class Seed:
    manager: SiphonManager
    cell_id: UUID
    broker_id: UUID
    target: CellTreasuryConfig
    snapshot: BrokerCashSnapshot
    option: Instrument


def seed(session: Session, *, settled_cash: Decimal = Decimal("200"), with_target: bool = True) -> Seed:
    broker_id, cell_id, target_id, option_id = uuid4(), uuid4(), uuid4(), uuid4()
    session.add(
        BrokerAccount(
            broker_account_id=broker_id,
            account_key=f"siphon-{broker_id}",
            broker_name="TEST",
            environment="PAPER",
            status="ACTIVE",
            effective_from=NOW,
        )
    )
    target = Instrument(
        instrument_id=target_id,
        symbol=f"META-{str(target_id)[:8]}",
        asset_class="EQUITY",
        currency="USD",
        effective_from=NOW,
    )
    option = Instrument(
        instrument_id=option_id,
        symbol=f"OPT-{str(option_id)[:8]}",
        asset_class="OPTION",
        currency="USD",
        underlying_symbol="TQQQ",
        contract_symbol=f"TQQQ{option_id.hex[:16]}",
        expiration_date=NOW.date(),
        strike_price=Decimal("50"),
        option_right="CALL",
        contract_multiplier=Decimal("100"),
        listing_type="STANDARD",
        effective_from=NOW,
    )
    session.add_all([target, option])
    strategy = session.get(StrategyRegistry, ("EMA-CROSS-001", "1.0.0"))
    assert strategy is not None
    session.add(
        CapitalCell(
            cell_id=cell_id,
            cell_code=f"CELL-{str(cell_id)[:8]}",
            seed_capital=Decimal("100"),
            status="ACTIVE",
            autonomy_tier="APPRENTICE",
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.version_tag,
            target_treasury_code=target.symbol,
            updated_at=NOW,
        )
    )
    session.flush()
    manager = SiphonManager(session)
    config = CellTreasuryConfig(
        config_id=uuid4(),
        cell_id=cell_id,
        target_type="SINGLE_ASSET",
        target_instrument_id=target_id,
        target_symbol=target.symbol,
        config_version=1,
        is_active=True,
        authorized_by="OWNER",
        created_at=NOW,
    )
    if with_target:
        session.add(config)
    snapshot = BrokerCashSnapshot(
        snapshot_id=uuid4(),
        broker_account_id=broker_id,
        broker_cash=settled_cash,
        settled_cash=settled_cash,
        unsettled_cash=Decimal("0"),
        buying_power=settled_cash,
        currency="USD",
        captured_at=NOW,
    )
    session.add(snapshot)
    session.flush()
    return Seed(manager, cell_id, broker_id, config, snapshot, option)


def add_profit(
    session: Session,
    seeded: Seed,
    amount: Decimal,
    *,
    effect: str = "CLOSING",
    simulated: bool = False,
    occurred_at: datetime | None = None,
) -> Fill:
    at = occurred_at or (NOW - timedelta(minutes=1))
    intent = OrderIntent(
        intent_id=uuid4(),
        cell_id=seeded.cell_id,
        strategy_id="EMA-CROSS-001",
        strategy_version="1.0.0",
        instrument_id=seeded.option.instrument_id,
        client_order_key=f"siphon-intent-{uuid4()}",
        order_purpose="ENTRY" if effect == "OPENING" else "TAKE_PROFIT",
        side="BUY" if effect == "OPENING" else "SELL",
        target_notional_usd=None,
        target_quantity=Decimal("1"),
        order_type="MARKET",
        created_at=at,
    )
    order = KairoOrder(
        kairo_order_id=uuid4(),
        intent_id=intent.intent_id,
        broker_account_id=seeded.broker_id,
        broker_order_id=f"ORDER-{uuid4()}",
        status="FILLED",
        submitted_at=at,
    )
    session.add(intent)
    session.flush()
    session.add(order)
    session.flush()
    source_snapshot_id = None
    metadata: dict = {}
    if simulated:
        market = MarketSnapshot(
            snapshot_id=uuid4(),
            instrument_id=seeded.option.instrument_id,
            captured_at=at,
            bid=Decimal("1.00"),
            ask=Decimal("1.02"),
            last=Decimal("1.01"),
            payload={"source": "TEST"},
        )
        session.add(market)
        source_snapshot_id = market.snapshot_id
        metadata = {"synthetic": True, "execution_guaranteed": False}
    fill = Fill(
        fill_id=uuid4(),
        kairo_order_id=order.kairo_order_id,
        broker_account_id=seeded.broker_id,
        broker_fill_id=f"FILL-{uuid4()}",
        instrument_id=seeded.option.instrument_id,
        side=intent.side,
        quantity=Decimal("1"),
        price=Decimal("1"),
        reference_price=Decimal("1") if simulated else None,
        contract_multiplier=Decimal("100") if simulated else None,
        slippage_usd=Decimal("0") if simulated else None,
        commission_fee_usd=Decimal("0"),
        is_simulated=simulated,
        liquidity_fidelity_tier="TIER_3_BAR_ONLY" if simulated else None,
        simulation_model="COARSE" if simulated else None,
        simulation_policy_version="1.0" if simulated else None,
        source_snapshot_id=source_snapshot_id,
        simulation_metadata=metadata,
        filled_at=at,
    )
    session.add(fill)
    session.flush()
    seeded.manager.record_canonical_realized_pnl(
        fill_id=fill.fill_id,
        cell_id=seeded.cell_id,
        position_effect=effect,
        realized_pnl_usd=amount,
        occurred_at=at,
    )
    return fill


def allocate_live(seeded: Seed, **kwargs):
    return seeded.manager.qualify_and_allocate(
        cell_id=seeded.cell_id,
        occurred_at=kwargs.pop("occurred_at", NOW + timedelta(minutes=1)),
        broker_account_id=kwargs.pop("broker_account_id", seeded.broker_id),
        settlement_snapshot_id=kwargs.pop("settlement_snapshot_id", seeded.snapshot.snapshot_id),
        **kwargs,
    )


def synthetic_metadata() -> SyntheticSettlementMetadata:
    return SyntheticSettlementMetadata(
        synthetic_settled_at=NOW,
        replay_session_id=f"REPLAY-{uuid4()}",
    )


def test_opening_fill_has_zero_siphonable_realized_profit(db_session: Session) -> None:
    s = seed(db_session)
    add_profit(db_session, s, Decimal("0"), effect="OPENING")
    assert allocate_live(s) is None


def test_closing_fill_uses_canonical_realized_pnl_as_attribution_ceiling(db_session: Session) -> None:
    s = seed(db_session)
    add_profit(db_session, s, Decimal("12"))
    result = allocate_live(s)
    assert result and result.qualified_profit_usd == Decimal("12.00")


def test_partial_closes_create_separate_realized_profit_tranches(db_session: Session) -> None:
    s = seed(db_session)
    add_profit(db_session, s, Decimal("6"))
    add_profit(db_session, s, Decimal("6"), occurred_at=NOW - timedelta(seconds=30))
    result = allocate_live(s)
    assert result and len(result.source_fill_ids) == 2


def test_siphon_manager_does_not_recalculate_trade_pnl_independently(db_session: Session) -> None:
    s = seed(db_session)
    fill = add_profit(db_session, s, Decimal("11"))
    fill.price = Decimal("999")  # Canonical fact, not fill-price arithmetic, remains authority.
    result = allocate_live(s)
    assert result and result.qualified_profit_usd == Decimal("11.00")


def test_unrealized_profit_is_not_siphonable(db_session: Session) -> None:
    assert allocate_live(seed(db_session)) is None


def test_unsettled_realized_profit_is_not_siphonable(db_session: Session) -> None:
    s = seed(db_session)
    add_profit(db_session, s, Decimal("20"), occurred_at=NOW + timedelta(minutes=1))
    assert allocate_live(s, occurred_at=NOW + timedelta(minutes=2)) is None


def test_seed_reference_cannot_be_siphoned(db_session: Session) -> None:
    s = seed(db_session, settled_cash=Decimal("105"))
    add_profit(db_session, s, Decimal("20"))
    assert allocate_live(s) is None


def test_committed_cash_is_not_siphonable(db_session: Session) -> None:
    s = seed(db_session, settled_cash=Decimal("130"))
    add_profit(db_session, s, Decimal("30"))
    assert allocate_live(s, committed_order_cash_usd=Decimal("25")) is None


def test_profit_below_threshold_does_not_create_siphon(db_session: Session) -> None:
    s = seed(db_session)
    add_profit(db_session, s, Decimal("9.99"))
    assert allocate_live(s) is None


def test_profit_at_threshold_creates_siphon(db_session: Session) -> None:
    s = seed(db_session)
    add_profit(db_session, s, Decimal("10"))
    assert allocate_live(s) is not None


def test_entire_qualified_surplus_is_allocated_once_threshold_reached(db_session: Session) -> None:
    s = seed(db_session)
    add_profit(db_session, s, Decimal("15"))
    result = allocate_live(s)
    assert result and result.qualified_profit_usd == Decimal("15.00")


def test_40_40_20_allocations_sum_exactly_to_source_profit(db_session: Session) -> None:
    s = seed(db_session)
    add_profit(db_session, s, Decimal("10"))
    result = allocate_live(s)
    assert result and (result.safety_reserve_usd, result.target_treasury_usd, result.replication_pool_usd) == (Decimal("4.00"), Decimal("4.00"), Decimal("2.00"))


def test_rounding_remainder_is_deterministically_assigned(db_session: Session) -> None:
    s = seed(db_session)
    add_profit(db_session, s, Decimal("10.03"))
    result = allocate_live(s)
    assert result and result.replication_pool_usd == Decimal("2.03")


def test_partial_profit_attribution_preserves_unsiphoned_remainder(db_session: Session) -> None:
    s = seed(db_session, settled_cash=Decimal("110"))
    fill = add_profit(db_session, s, Decimal("20"))
    result = allocate_live(s)
    used = db_session.scalar(select(func.sum(SiphonProfitAttribution.attributed_profit_usd)).where(SiphonProfitAttribution.source_fill_id == fill.fill_id))
    assert result and Decimal(used) == Decimal("10.00")


def test_prior_attributed_profit_cannot_be_siphoned_twice(db_session: Session) -> None:
    s = seed(db_session)
    add_profit(db_session, s, Decimal("10"))
    assert allocate_live(s) is not None
    assert allocate_live(s, occurred_at=NOW + timedelta(minutes=2)) is None


def test_multiple_siphons_can_consume_distinct_portions_of_same_profit_source(db_session: Session) -> None:
    s = seed(db_session, settled_cash=Decimal("110"))
    fill = add_profit(db_session, s, Decimal("30"))
    assert allocate_live(s) is not None
    later = BrokerCashSnapshot(snapshot_id=uuid4(), broker_account_id=s.broker_id, broker_cash=Decimal("120"), settled_cash=Decimal("120"), unsettled_cash=Decimal("0"), buying_power=Decimal("120"), currency="USD", captured_at=NOW + timedelta(minutes=2))
    db_session.add(later)
    db_session.flush()
    second = s.manager.qualify_and_allocate(cell_id=s.cell_id, occurred_at=NOW + timedelta(minutes=3), broker_account_id=s.broker_id, settlement_snapshot_id=later.snapshot_id)
    count = db_session.scalar(select(func.count()).select_from(SiphonProfitAttribution).where(SiphonProfitAttribution.source_fill_id == fill.fill_id))
    assert second and count == 2


def test_total_attributed_profit_never_exceeds_source_realized_profit(db_session: Session) -> None:
    s = seed(db_session)
    fill = add_profit(db_session, s, Decimal("10"))
    result = allocate_live(s)
    assert result
    db_session.add(SiphonProfitAttribution(attribution_id=uuid4(), siphon_id=result.siphon_id, source_fill_id=fill.fill_id, attributed_profit_usd=Decimal("0.01"), occurred_at=NOW + timedelta(minutes=2)))
    with pytest.raises(DBAPIError):
        db_session.flush()


def test_siphon_event_attribution_sum_equals_qualified_profit(db_session: Session) -> None:
    s = seed(db_session)
    add_profit(db_session, s, Decimal("17"))
    result = allocate_live(s)
    assert result and sum(item.attributed_profit_usd for item in result.attributions) == result.qualified_profit_usd


def test_siphon_event_is_append_only(db_session: Session) -> None:
    s = seed(db_session)
    add_profit(db_session, s, Decimal("10"))
    result = allocate_live(s)
    assert result
    with pytest.raises(DBAPIError):
        db_session.execute(text("UPDATE siphon_events SET amount=11 WHERE siphon_id=:id"), {"id": result.siphon_id})


def test_siphon_does_not_create_treasury_market_order(db_session: Session) -> None:
    s = seed(db_session)
    add_profit(db_session, s, Decimal("10"))
    before = db_session.scalar(select(func.count()).select_from(OrderIntent).where(OrderIntent.order_purpose == "TREASURY_PURCHASE"))
    allocate_live(s)
    after = db_session.scalar(select(func.count()).select_from(OrderIntent).where(OrderIntent.order_purpose == "TREASURY_PURCHASE"))
    assert before == after == 0


def test_siphon_does_not_change_risk_governor_session_pnl(db_session: Session) -> None:
    s = seed(db_session)
    add_profit(db_session, s, Decimal("10"))
    before = list(db_session.execute(select(RiskGovernorState.session_id, RiskGovernorState.session_net_pnl)))
    allocate_live(s)
    assert list(db_session.execute(select(RiskGovernorState.session_id, RiskGovernorState.session_net_pnl))) == before


def test_paper_profit_can_create_only_synthetic_siphon_evidence(db_session: Session) -> None:
    s = seed(db_session)
    add_profit(db_session, s, Decimal("10"), simulated=True)
    assert allocate_live(s) is None
    result = s.manager.qualify_and_allocate(cell_id=s.cell_id, occurred_at=NOW + timedelta(minutes=1), broker_account_id=s.broker_id, synthetic_settled_cash_usd=Decimal("200"), synthetic_settlement_metadata=synthetic_metadata())
    assert result and result.is_synthetic and result.settlement_snapshot_id is None


def test_only_one_active_target_config_per_cell(db_session: Session) -> None:
    s = seed(db_session)
    db_session.add(CellTreasuryConfig(config_id=uuid4(), cell_id=s.cell_id, target_type="SINGLE_ASSET", target_instrument_id=s.target.target_instrument_id, target_symbol=s.target.target_symbol, config_version=2, is_active=True, authorized_by="OWNER", created_at=NOW))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_multiple_inactive_target_versions_are_allowed(db_session: Session) -> None:
    s = seed(db_session)
    s.target.is_active = False
    db_session.flush()
    for version in (2, 3):
        db_session.add(CellTreasuryConfig(config_id=uuid4(), cell_id=s.cell_id, target_type="SINGLE_ASSET", target_instrument_id=s.target.target_instrument_id, target_symbol=s.target.target_symbol, config_version=version, is_active=False, authorized_by="OWNER", created_at=NOW + timedelta(seconds=version)))
    db_session.flush()


def test_target_config_versions_are_unique_per_cell(db_session: Session) -> None:
    s = seed(db_session)
    s.target.is_active = False
    db_session.flush()
    db_session.add(CellTreasuryConfig(config_id=uuid4(), cell_id=s.cell_id, target_type="SINGLE_ASSET", target_instrument_id=s.target.target_instrument_id, target_symbol=s.target.target_symbol, config_version=1, is_active=False, authorized_by="OWNER", created_at=NOW))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_v1_rejects_unsupported_future_target_type(db_session: Session) -> None:
    s = seed(db_session)
    s.target.target_type = "BASKET"
    db_session.flush()
    add_profit(db_session, s, Decimal("10"))
    with pytest.raises(ValueError, match="SINGLE_ASSET"):
        allocate_live(s)


def test_target_instrument_must_resolve_canonically(db_session: Session) -> None:
    s = seed(db_session)
    with pytest.raises(ValueError, match="canonical"):
        s.manager.create_target_config(CellTreasuryConfigInput(cell_id=s.cell_id, target_instrument_id=uuid4(), target_symbol="MISSING", config_version=2, authorized_by="OWNER", created_at=NOW))


def test_no_implicit_meta_target_default(db_session: Session) -> None:
    s = seed(db_session, with_target=False)
    add_profit(db_session, s, Decimal("10"))
    with pytest.raises(ValueError, match="no active"):
        allocate_live(s)


def test_live_settlement_snapshot_matches_broker_account(db_session: Session) -> None:
    s = seed(db_session)
    add_profit(db_session, s, Decimal("10"))
    with pytest.raises(ValueError, match="does not match"):
        allocate_live(s, broker_account_id=uuid4())


def test_synthetic_siphon_requires_synthetic_settlement_provenance(db_session: Session) -> None:
    s = seed(db_session)
    add_profit(db_session, s, Decimal("10"), simulated=True)
    with pytest.raises(ValueError, match="live settlement"):
        s.manager.qualify_and_allocate(cell_id=s.cell_id, occurred_at=NOW + timedelta(minutes=1), synthetic_settled_cash_usd=Decimal("200"))


def _invalid_event(s: Seed, **changes) -> SiphonEvent:
    values = dict(siphon_id=uuid4(), cell_id=s.cell_id, treasury_code=s.target.target_symbol, amount=Decimal("10"), occurred_at=NOW, reason_code="TEST", policy_id="PROFIT-ALLOC-v1.0", policy_version="1.0.0", broker_account_id=s.broker_id, settlement_snapshot_id=s.snapshot.snapshot_id, source_fill_ids=[], qualified_profit_usd=Decimal("10"), safety_reserve_usd=Decimal("4"), target_treasury_usd=Decimal("4"), replication_pool_usd=Decimal("2"), target_config_id=s.target.config_id, is_synthetic=False, synthetic_settlement_metadata=None, source_manifest_hash="a" * 64)
    values.update(changes)
    return SiphonEvent(**values)


def test_database_rejects_synthetic_siphon_with_live_settlement_snapshot(db_session: Session) -> None:
    s = seed(db_session)
    db_session.add(_invalid_event(s, is_synthetic=True, synthetic_settlement_metadata=synthetic_metadata().model_dump(mode="json")))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_database_rejects_live_siphon_without_settlement_snapshot(db_session: Session) -> None:
    s = seed(db_session)
    db_session.add(_invalid_event(s, settlement_snapshot_id=None))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_database_rejects_siphon_allocation_sum_mismatch(db_session: Session) -> None:
    s = seed(db_session)
    db_session.add(_invalid_event(s, replication_pool_usd=Decimal("1")))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_treasury_config_rejects_timezone_naive_created_at() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        CellTreasuryConfigInput(cell_id=uuid4(), target_instrument_id=uuid4(), target_symbol="META", authorized_by="OWNER", created_at=datetime(2026, 1, 1))


def test_migration_0011_upgrade_and_downgrade_preserve_existing_lineage(migrated_database: tuple[str, str]) -> None:
    admin_url, _ = migrated_database
    backend_root = Path(__file__).resolve().parents[3]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    engine = create_engine(admin_url)
    command.downgrade(config, "0010")
    legacy_id, legacy_cell = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO siphon_events (siphon_id, cell_id, treasury_code, amount, occurred_at, reason_code) VALUES (:id, :cell, 'LEGACY', 12.34, :at, 'LEGACY')"), {"id": legacy_id, "cell": legacy_cell, "at": NOW})
    command.upgrade(config, "0011")
    with engine.connect() as connection:
        row = connection.execute(text("SELECT policy_id, qualified_profit_usd, replication_pool_usd FROM siphon_events WHERE siphon_id=:id"), {"id": legacy_id}).one()
        assert row == ("LEGACY-SIPHON-v0", Decimal("12.34"), Decimal("12.34"))
    command.downgrade(config, "0010")
    assert "policy_id" not in {column["name"] for column in inspect(engine).get_columns("siphon_events")}
    command.upgrade(config, "0011")
    engine.dispose()
