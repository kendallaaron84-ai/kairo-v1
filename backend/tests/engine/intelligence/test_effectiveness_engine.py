import hashlib
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect as sa_inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.models.broker import BrokerAccount
from app.db.models.configuration import Instrument, StrategyRegistry
from app.db.models.intelligence import (
    IntelligenceResearchCategorySlice,
    IntelligenceResearchRun,
    MarketContextAssessment,
    OrderContextEvaluation,
)
from app.db.models.ledger import Fill, FillRealizedPnL, KairoOrder, OrderIntent
from app.db.models.projections import CapitalCell
from engine.intelligence.context.context_gate import ContextGate
from engine.intelligence.research.effectiveness_engine import EffectivenessEngine
from engine.intelligence.research.models import (
    RESEARCH_SEMANTICS,
    ResearchMethod,
    serialize_research_manifest,
)


pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)
START = NOW - timedelta(hours=1)
END = NOW + timedelta(hours=1)
DEFAULT_POLICY_ID = UUID("a0000000-0000-0000-0000-000000000001")


def seed_research_context(
    session: Session,
) -> tuple[CapitalCell, BrokerAccount, Instrument, StrategyRegistry]:
    suffix = uuid4().hex[:12]
    strategy = StrategyRegistry(
        strategy_id=f"RESEARCH-{suffix}", version_tag="1.0.0",
        display_name="Step 5 research fixture", status="ACTIVE",
        configuration={"clearance": "PAPER_ONLY"},
    )
    instrument = Instrument(
        instrument_id=uuid4(), symbol=f"R{suffix[:8]}",
        asset_class="EQUITY", currency="USD",
    )
    broker = BrokerAccount(
        broker_account_id=uuid4(), account_key=f"research-{suffix}",
        broker_name="RESEARCH_FIXTURE", environment="PAPER", status="ACTIVE",
        effective_from=START,
    )
    session.add_all([strategy, instrument, broker])
    session.flush()
    cell = CapitalCell(
        cell_id=uuid4(), cell_code=f"CELL-{suffix}",
        seed_capital=Decimal("1000.00"), status="ACTIVE",
        autonomy_tier="APPRENTICE", strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag, target_treasury_code="QQQ",
        risk_policy_id=DEFAULT_POLICY_ID, economic_domain="SYNTHETIC",
    )
    session.add(cell)
    session.flush()
    return cell, broker, instrument, strategy


def add_closed_trade(
    session: Session,
    *,
    cell: CapitalCell,
    broker: BrokerAccount,
    instrument: Instrument,
    strategy: StrategyRegistry,
    pnl: str,
    close_at: datetime,
    opinion: str | None = None,
    reason: str | None = None,
    causal_valid: bool = True,
    intent_id: UUID | None = None,
    fill_id: UUID | None = None,
) -> Fill:
    intent_id = intent_id or uuid4()
    fill_id = fill_id or uuid4()
    intent_created_at = close_at - timedelta(minutes=5)
    intent = OrderIntent(
        intent_id=intent_id, cell_id=cell.cell_id,
        strategy_id=strategy.strategy_id, strategy_version=strategy.version_tag,
        instrument_id=instrument.instrument_id,
        client_order_key=f"research-{intent_id}",
        order_purpose="TAKE_PROFIT" if Decimal(pnl) >= 0 else "STOP_LOSS",
        side="SELL", target_quantity=Decimal("1"), order_type="MARKET",
        created_at=intent_created_at,
    )
    session.add(intent)
    session.flush()
    order = KairoOrder(
        kairo_order_id=uuid4(), intent_id=intent.intent_id,
        risk_decision_id=None, broker_account_id=broker.broker_account_id,
        broker_order_id=f"RESEARCH-{fill_id}", status="FILLED",
        submitted_at=close_at,
    )
    session.add(order)
    session.flush()
    fill = Fill(
        fill_id=fill_id, kairo_order_id=order.kairo_order_id,
        broker_account_id=broker.broker_account_id,
        broker_fill_id=f"RESEARCH-FILL-{fill_id}",
        instrument_id=instrument.instrument_id, side="SELL",
        quantity=Decimal("1"), price=Decimal("1"),
        commission_fee_usd=Decimal("0"), is_simulated=False,
        simulation_metadata={}, filled_at=close_at,
    )
    session.add(fill)
    session.flush()
    session.add(FillRealizedPnL(
        realization_id=uuid4(), fill_id=fill.fill_id, cell_id=cell.cell_id,
        position_effect="CLOSING", realized_pnl_usd=Decimal(pnl),
        source_authority="TEST_CANONICAL_ACCOUNTING", occurred_at=close_at,
    ))
    session.flush()

    if opinion is not None:
        evaluation_at = (
            intent_created_at - timedelta(seconds=1)
            if causal_valid else intent_created_at + timedelta(seconds=1)
        )
        assessment = MarketContextAssessment(
            assessment_id=uuid4(), cell_id=cell.cell_id,
            risk_posture="ELEVATED" if opinion == "WOULD_HAVE_VETOED" else "NORMAL",
            authority_mode="OBSERVE_ONLY", macro_window_active=False,
            primary_event_id=None, active_case_id=None,
            assessment_summary="Immutable Step 5 fixture assessment.",
            assessment_manifest_sha256=hashlib.sha256(str(intent_id).encode()).hexdigest(),
            evaluated_at=evaluation_at, created_at=evaluation_at,
        )
        session.add(assessment)
        session.flush()
        session.add(OrderContextEvaluation(
            evaluation_id=uuid4(), intent_id=intent.intent_id,
            assessment_id=assessment.assessment_id,
            counterfactual_opinion=opinion, veto_reason_code=reason,
            evaluated_at=evaluation_at,
        ))
        session.flush()
    return fill


def run(session: Session, cell: CapitalCell | None = None) -> IntelligenceResearchRun:
    return EffectivenessEngine(session, clock=lambda: NOW).run_trade_removal_analysis(
        cell.cell_id if cell else None, START, END
    )


def test_large_baseline_with_tiny_veto_population_is_not_marked_sufficient(
    db_session: Session,
) -> None:
    cell, broker, instrument, strategy = seed_research_context(db_session)
    for index in range(20):
        add_closed_trade(
            db_session, cell=cell, broker=broker, instrument=instrument,
            strategy=strategy, pnl="-1.00" if index == 0 else "1.00",
            close_at=NOW + timedelta(minutes=index),
            opinion="WOULD_HAVE_VETOED" if index == 0 else None,
            reason="CRITICAL_MACRO_WINDOW" if index == 0 else None,
        )
    engine = EffectivenessEngine(db_session, clock=lambda: NOW)
    fact = engine.run_trade_removal_analysis(cell.cell_id, START, END)
    assert fact.total_baseline_trades == 20
    assert fact.total_veto_opportunities == 1
    assert engine.last_manifest["research_semantics"]["sample_sufficiency"] == "NOT_ASSESSED"
    assert "SUFFICIENT" not in serialize_research_manifest(engine.last_manifest).decode()


def test_research_run_records_context_evaluated_and_veto_population_separately(
    db_session: Session,
) -> None:
    cell, broker, instrument, strategy = seed_research_context(db_session)
    cases = [
        ("1.00", None, None, True),
        ("2.00", "WOULD_HAVE_AUTHORIZED", None, True),
        ("-3.00", "WOULD_HAVE_VETOED", "CRITICAL_MACRO_WINDOW", True),
        ("-4.00", "WOULD_HAVE_VETOED", "STALE_CASE_CONCERN", False),
    ]
    for index, (pnl, opinion, reason, valid) in enumerate(cases):
        add_closed_trade(
            db_session, cell=cell, broker=broker, instrument=instrument,
            strategy=strategy, pnl=pnl, close_at=NOW + timedelta(minutes=index),
            opinion=opinion, reason=reason, causal_valid=valid,
        )
    fact = run(db_session, cell)
    assert fact.total_baseline_trades == 4
    assert fact.total_context_evaluated_trades == 2
    assert fact.total_veto_opportunities == 1
    assert fact.excluded_causal_invalid_trades == 1


def test_trade_removal_counterfactual_is_explicitly_labeled(db_session: Session) -> None:
    fact = run(db_session)
    assert fact.research_method == "TRADE_REMOVAL_COUNTERFACTUAL"
    assert ResearchMethod(fact.research_method) is ResearchMethod.TRADE_REMOVAL_COUNTERFACTUAL


def test_research_does_not_claim_stateful_replay_equivalence(db_session: Session) -> None:
    engine = EffectivenessEngine(db_session, clock=lambda: NOW)
    engine.run_trade_removal_analysis(None, START, END)
    semantics = engine.last_manifest["research_semantics"]
    assert semantics["descriptive_only"] is True
    assert semantics["claims_stateful_replay_equivalence"] is False
    assert semantics["claims_statistical_significance"] is False
    assert semantics["subsequent_state_changes_modeled"] is False


def test_drawdown_uses_canonical_trade_close_order(db_session: Session) -> None:
    cell, broker, instrument, strategy = seed_research_context(db_session)
    for pnl, minute in [("3.00", 2), ("10.00", 0), ("-8.00", 1)]:
        add_closed_trade(
            db_session, cell=cell, broker=broker, instrument=instrument,
            strategy=strategy, pnl=pnl, close_at=NOW + timedelta(minutes=minute),
        )
    engine = EffectivenessEngine(db_session, clock=lambda: NOW)
    fact = engine.run_trade_removal_analysis(cell.cell_id, START, END)
    assert fact.baseline_max_drawdown_usd == Decimal("8.00")
    assert [t["realized_pnl"] for t in engine.last_manifest["trade_facts"]] == [
        "10.00", "-8.00", "3.00"
    ]


def test_causal_invalid_trades_are_counted_and_excluded(db_session: Session) -> None:
    cell, broker, instrument, strategy = seed_research_context(db_session)
    add_closed_trade(
        db_session, cell=cell, broker=broker, instrument=instrument,
        strategy=strategy, pnl="-5.00", close_at=NOW,
        opinion="WOULD_HAVE_VETOED", reason="CRITICAL_MACRO_WINDOW",
        causal_valid=False,
    )
    add_closed_trade(
        db_session, cell=cell, broker=broker, instrument=instrument,
        strategy=strategy, pnl="2.00", close_at=NOW + timedelta(minutes=1),
        opinion="WOULD_HAVE_AUTHORIZED",
    )
    fact = run(db_session, cell)
    assert fact.excluded_causal_invalid_trades == 1
    assert fact.total_context_evaluated_trades == 1
    assert fact.total_veto_opportunities == 0
    assert fact.baseline_net_pnl == fact.counterfactual_net_pnl == Decimal("-3.00")


def test_step5_cannot_promote_context_gate_authority(db_session: Session) -> None:
    before = ContextGate.authority_mode
    run(db_session)
    assert before == ContextGate.authority_mode == "OBSERVE_ONLY"
    assert RESEARCH_SEMANTICS["authority_mode"] == "OFFLINE_RESEARCH_ONLY"


def test_research_engine_reconciles_exact_cent_differential_math(
    db_session: Session,
) -> None:
    cell, broker, instrument, strategy = seed_research_context(db_session)
    for index, (pnl, opinion) in enumerate([
        ("10.01", "WOULD_HAVE_VETOED"),
        ("-4.03", "WOULD_HAVE_VETOED"),
        ("2.02", "WOULD_HAVE_AUTHORIZED"),
    ]):
        add_closed_trade(
            db_session, cell=cell, broker=broker, instrument=instrument,
            strategy=strategy, pnl=pnl, close_at=NOW + timedelta(minutes=index),
            opinion=opinion,
            reason="CRITICAL_MACRO_WINDOW" if opinion == "WOULD_HAVE_VETOED" else None,
        )
    fact = run(db_session, cell)
    assert fact.baseline_net_pnl == Decimal("8.00")
    assert fact.counterfactual_net_pnl == Decimal("2.02")
    assert fact.losses_avoided_usd == Decimal("4.03")
    assert fact.profits_forfeited_usd == Decimal("10.01")
    assert fact.net_alpha_usd == Decimal("-5.98")
    assert fact.baseline_net_pnl == fact.counterfactual_net_pnl - fact.net_alpha_usd


def test_vetoed_winning_trades_accounted_as_profits_forfeited(db_session: Session) -> None:
    cell, broker, instrument, strategy = seed_research_context(db_session)
    add_closed_trade(
        db_session, cell=cell, broker=broker, instrument=instrument,
        strategy=strategy, pnl="3.21", close_at=NOW,
        opinion="WOULD_HAVE_VETOED", reason="CRITICAL_MACRO_WINDOW",
    )
    fact = run(db_session, cell)
    assert fact.vetoed_winning_trades == 1
    assert fact.profits_forfeited_usd == Decimal("3.21")


def test_vetoed_losing_trades_accounted_as_losses_avoided(db_session: Session) -> None:
    cell, broker, instrument, strategy = seed_research_context(db_session)
    add_closed_trade(
        db_session, cell=cell, broker=broker, instrument=instrument,
        strategy=strategy, pnl="-2.34", close_at=NOW,
        opinion="WOULD_HAVE_VETOED", reason="CRITICAL_MACRO_WINDOW",
    )
    fact = run(db_session, cell)
    assert fact.vetoed_losing_trades == 1
    assert fact.losses_avoided_usd == Decimal("2.34")


def test_research_engine_slices_effectiveness_by_event_category(db_session: Session) -> None:
    cell, broker, instrument, strategy = seed_research_context(db_session)
    for index, (pnl, reason) in enumerate([
        ("-5.00", "CRITICAL_MACRO_WINDOW"),
        ("2.00", "STALE_CASE_CONCERN"),
    ]):
        add_closed_trade(
            db_session, cell=cell, broker=broker, instrument=instrument,
            strategy=strategy, pnl=pnl, close_at=NOW + timedelta(minutes=index),
            opinion="WOULD_HAVE_VETOED", reason=reason,
        )
    fact = run(db_session, cell)
    slices = db_session.scalars(
        select(IntelligenceResearchCategorySlice)
        .where(IntelligenceResearchCategorySlice.run_id == fact.run_id)
        .order_by(IntelligenceResearchCategorySlice.category_code)
    ).all()
    assert [row.category_code for row in slices] == [
        "CRITICAL_MACRO_WINDOW", "STALE_CASE_CONCERN"
    ]
    assert [row.slice_net_alpha_usd for row in slices] == [
        Decimal("5.00"), Decimal("-2.00")
    ]


def test_research_manifest_hash_is_deterministic_and_byte_exact(db_session: Session) -> None:
    first = EffectivenessEngine(db_session, clock=lambda: NOW)
    first_fact = first.run_trade_removal_analysis(None, START, END)
    first_bytes = serialize_research_manifest(first.last_manifest)
    assert hashlib.sha256(first_bytes).hexdigest() == first_fact.research_manifest_sha256
    assert first_fact.run_id == UUID("fed5152f-66e1-53bc-8d23-a77a80a65b7d")
    assert first_fact.research_manifest_sha256 == (
        "06cefd2200b4b6ccdb9c4028bdf4d41688cd9415661bcb70c46617dddb89f183"
    )
    second = EffectivenessEngine(db_session, clock=lambda: NOW + timedelta(days=1))
    second_fact = second.run_trade_removal_analysis(None, START, END)
    assert second_fact.run_id == first_fact.run_id
    assert second_fact.research_manifest_sha256 == first_fact.research_manifest_sha256
    assert serialize_research_manifest(second.last_manifest) == first_bytes


def test_database_immutability_rejects_update_or_delete_on_research_runs(
    db_session: Session,
) -> None:
    fact = run(db_session)
    with pytest.raises(DBAPIError, match="immutable"), db_session.begin_nested():
        db_session.execute(
            text("UPDATE intelligence_research_runs SET net_alpha_usd = 1 WHERE run_id = :id"),
            {"id": fact.run_id},
        )
    with pytest.raises(DBAPIError, match="immutable"), db_session.begin_nested():
        db_session.execute(
            text("DELETE FROM intelligence_research_runs WHERE run_id = :id"),
            {"id": fact.run_id},
        )


def test_zero_runtime_trade_authority_or_governor_leakage_from_research_engine(
    db_session: Session,
) -> None:
    intent_count = db_session.scalar(select(func.count()).select_from(OrderIntent))
    source = inspect.getsource(EffectivenessEngine)
    run(db_session)
    assert db_session.scalar(select(func.count()).select_from(OrderIntent)) == intent_count
    assert EffectivenessEngine.authority_mode == "OFFLINE_RESEARCH_ONLY"
    assert "RiskGovernor" not in source
    assert "ContextGate" not in source
    assert "VETO_ONLY" not in source


def test_migration_0020_upgrade_and_downgrade_are_clean_and_data_safe(
    migrated_database: tuple[str, str],
) -> None:
    admin_url, _ = migrated_database
    backend_root = Path(__file__).resolve().parents[3]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    command.downgrade(config, "0019")
    engine = create_engine(admin_url)
    try:
        assert "intelligence_research_runs" not in sa_inspect(engine).get_table_names()
        command.upgrade(config, "0020")
        assert "intelligence_research_runs" in sa_inspect(engine).get_table_names()
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO intelligence_research_runs (
                    run_id, cell_id, research_method, sample_start_time,
                    sample_end_time, total_baseline_trades,
                    total_context_evaluated_trades, total_veto_opportunities,
                    vetoed_losing_trades, vetoed_winning_trades,
                    vetoed_breakeven_trades, excluded_causal_invalid_trades,
                    baseline_net_pnl, counterfactual_net_pnl,
                    losses_avoided_usd, profits_forfeited_usd, net_alpha_usd,
                    baseline_max_drawdown_usd, counterfactual_max_drawdown_usd,
                    veto_precision_pct, research_manifest_sha256, executed_at
                ) VALUES (
                    :id, NULL, 'TRADE_REMOVAL_COUNTERFACTUAL', :start, :end,
                    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                    :hash, :executed
                )
            """), {
                "id": uuid4(), "start": START, "end": END,
                "hash": "0" * 64, "executed": NOW,
            })
        with pytest.raises(Exception, match="Refusing 0020 downgrade"):
            command.downgrade(config, "0019")
        with engine.begin() as connection:
            connection.execute(text(
                "TRUNCATE intelligence_research_category_slices, intelligence_research_runs"
            ))
        command.downgrade(config, "0019")
        command.upgrade(config, "head")
    finally:
        engine.dispose()
