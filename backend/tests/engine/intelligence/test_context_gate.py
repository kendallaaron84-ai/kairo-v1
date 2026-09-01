import inspect
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect as sa_inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.models.configuration import Instrument, StrategyRegistry
from app.db.models.intelligence import (
    IntelligenceEvidenceLedger,
    MarketContextAssessment,
    OrderContextEvaluation,
)
from app.db.models.ledger import OrderIntent
from app.db.models.projections import CapitalCell
from app.db.models.risk import RiskGovernorState, RiskSession
from engine.intelligence.cases.case_engine import CaseEngine
from engine.intelligence.context.context_gate import ContextGate
from engine.intelligence.evidence_store import EvidenceStore
from engine.intelligence.models import (
    EventType,
    ImpactScope,
    IntelligenceIngestPayload,
    ReleaseStatus,
    SourceType,
    TimeHorizon,
    UrgencyLevel,
)
from engine.intelligence.storage_driver import LocalContentAddressedStorage


pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 1, 17, 0, tzinfo=UTC)
DEFAULT_POLICY_ID = UUID("a0000000-0000-0000-0000-000000000001")


def seed_cell_and_intent(
    session: Session, *, created_at: datetime = NOW - timedelta(minutes=1)
) -> tuple[CapitalCell, OrderIntent]:
    suffix = uuid4().hex[:12]
    strategy = StrategyRegistry(
        strategy_id=f"CONTEXT-{suffix}",
        version_tag="1.0.0",
        display_name="Context observation fixture",
        status="ACTIVE",
        configuration={"clearance": "PAPER_ONLY"},
    )
    instrument = Instrument(
        instrument_id=uuid4(),
        symbol=f"CTX{suffix[:8]}",
        asset_class="EQUITY",
        currency="USD",
    )
    session.add_all([strategy, instrument])
    session.flush()
    cell = CapitalCell(
        cell_id=uuid4(),
        cell_code=f"CELL-{suffix}",
        seed_capital=Decimal("1000.00"),
        status="ACTIVE",
        autonomy_tier="APPRENTICE",
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag,
        target_treasury_code="QQQ",
        risk_policy_id=DEFAULT_POLICY_ID,
        economic_domain="SYNTHETIC",
    )
    session.add(cell)
    session.flush()
    intent = OrderIntent(
        intent_id=uuid4(),
        cell_id=cell.cell_id,
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag,
        instrument_id=instrument.instrument_id,
        client_order_key=f"context-{uuid4()}",
        order_purpose="ENTRY",
        side="BUY",
        target_quantity=Decimal("1"),
        order_type="MARKET",
        created_at=created_at,
    )
    session.add(intent)
    session.flush()
    return cell, intent


def add_macro_evidence(
    session: Session,
    root: Path,
    *,
    effective_at: datetime,
    observed_at: datetime,
    published_at: datetime | None = None,
    title: str = "FOMC policy decision",
) -> IntelligenceEvidenceLedger:
    store = EvidenceStore(
        session, LocalContentAddressedStorage(root), clock=lambda: observed_at
    )
    return store.ingest_evidence(IntelligenceIngestPayload(
        source_type=SourceType.PRIMARY,
        source_name="FEDERAL_RESERVE",
        source_uri=f"https://federalreserve.gov/{uuid4()}",
        event_type=EventType.MACRO,
        title=title,
        summary="Official critical macro event.",
        published_at=published_at or effective_at,
        effective_at=effective_at,
        observed_at=observed_at,
        impact_scope=ImpactScope.MARKET,
        urgency=UrgencyLevel.CRITICAL,
        confidence_score=Decimal("100.00"),
        time_horizon=TimeHorizon.INTRADAY,
        release_status=ReleaseStatus.SCHEDULED,
        raw_content_bytes=f"{title}-{uuid4()}".encode(),
    ))


def gate(session: Session) -> ContextGate:
    return ContextGate(session, clock=lambda: NOW)


def add_concern_case(session: Session, *, symbol: str = "JOBY") -> object:
    engine = CaseEngine(session, clock=lambda: NOW - timedelta(minutes=1))
    case = engine.open_case(
        "Is the certification milestone supported?",
        symbol,
        "EVTOL",
        "The milestone is supported.",
    )
    return engine.conclude_case(
        case.case_id,
        ImpactScope.COMPANY,
        TimeHorizon.MONTHS,
        "No qualifying evidence was available.",
        Decimal("0.00"),
    )


def test_macro_window_uses_effective_event_time_not_ingestion_time(
    db_session: Session, tmp_path: Path
) -> None:
    cell, _ = seed_cell_and_intent(db_session)
    event = add_macro_evidence(
        db_session,
        tmp_path,
        effective_at=NOW,
        observed_at=NOW - timedelta(hours=2),
        published_at=NOW - timedelta(days=10),
    )
    result = gate(db_session).evaluate_market_context(cell.cell_id, NOW)
    assert event.effective_at == NOW
    assert event.published_at != event.effective_at
    assert result.primary_event_id == event.event_id
    assert result.risk_posture == "HIGH_EVENT_RISK"


def test_context_gate_cannot_use_future_evidence(
    db_session: Session, tmp_path: Path
) -> None:
    cell, _ = seed_cell_and_intent(db_session)
    add_macro_evidence(
        db_session,
        tmp_path,
        effective_at=NOW,
        observed_at=NOW + timedelta(seconds=1),
    )
    result = gate(db_session).evaluate_market_context(cell.cell_id, NOW)
    assert result.risk_posture == "NORMAL"
    assert result.primary_event_id is None


def test_company_dossier_concern_produces_elevated_no_opinion_in_step4(
    db_session: Session,
) -> None:
    cell, intent = seed_cell_and_intent(db_session)
    conclusion = add_concern_case(db_session)
    context_gate = gate(db_session)
    assessment = context_gate.evaluate_market_context(cell.cell_id, NOW, "joby")
    evaluation = context_gate.evaluate_order_intent(intent.intent_id, assessment)
    assert assessment.risk_posture == "ELEVATED"
    assert assessment.active_case_id == conclusion.case_id
    assert evaluation.counterfactual_opinion == "NO_OPINION"
    assert evaluation.veto_reason_code is None


def test_database_rejects_cross_cell_order_context_binding(
    db_session: Session,
) -> None:
    first_cell, _ = seed_cell_and_intent(db_session)
    _, other_intent = seed_cell_and_intent(db_session)
    assessment = gate(db_session).evaluate_market_context(first_cell.cell_id, NOW)
    with pytest.raises(DBAPIError, match="Cross-cell"), db_session.begin_nested():
        db_session.add(OrderContextEvaluation(
            evaluation_id=uuid4(), intent_id=other_intent.intent_id,
            assessment_id=assessment.assessment_id,
            counterfactual_opinion="WOULD_HAVE_AUTHORIZED",
            evaluated_at=NOW,
        ))
        db_session.flush()


def test_order_context_evaluation_is_temporally_causal(
    db_session: Session,
) -> None:
    cell, intent = seed_cell_and_intent(db_session)
    assessment = gate(db_session).evaluate_market_context(cell.cell_id, NOW)
    with pytest.raises(DBAPIError, match="Temporal causality"), db_session.begin_nested():
        db_session.add(OrderContextEvaluation(
            evaluation_id=uuid4(), intent_id=intent.intent_id,
            assessment_id=assessment.assessment_id,
            counterfactual_opinion="WOULD_HAVE_AUTHORIZED",
            evaluated_at=NOW - timedelta(seconds=1),
        ))
        db_session.flush()


def test_context_assessment_detects_active_fomc_macro_window(
    db_session: Session, tmp_path: Path
) -> None:
    cell, _ = seed_cell_and_intent(db_session)
    event = add_macro_evidence(
        db_session, tmp_path, effective_at=NOW + timedelta(minutes=5),
        observed_at=NOW - timedelta(hours=1),
    )
    result = gate(db_session).evaluate_market_context(cell.cell_id, NOW)
    assert result.macro_window_active is True
    assert result.primary_event_id == event.event_id
    assert result.authority_mode == "OBSERVE_ONLY"


def test_context_assessment_records_normal_posture_when_clear(
    db_session: Session,
) -> None:
    cell, _ = seed_cell_and_intent(db_session)
    result = gate(db_session).evaluate_market_context(cell.cell_id, NOW, "QQQ")
    assert result.risk_posture == "NORMAL"
    assert result.macro_window_active is False
    assert result.active_case_id is None


def test_order_evaluation_records_would_have_vetoed_during_macro_window(
    db_session: Session, tmp_path: Path
) -> None:
    cell, intent = seed_cell_and_intent(db_session)
    add_macro_evidence(db_session, tmp_path, effective_at=NOW, observed_at=NOW)
    context_gate = gate(db_session)
    assessment = context_gate.evaluate_market_context(cell.cell_id, NOW)
    result = context_gate.evaluate_order_intent(intent.intent_id, assessment)
    assert result.counterfactual_opinion == "WOULD_HAVE_VETOED"
    assert result.veto_reason_code == "CRITICAL_MACRO_WINDOW_ACTIVE"


def test_order_evaluation_records_would_have_authorized_under_normal_posture(
    db_session: Session,
) -> None:
    cell, intent = seed_cell_and_intent(db_session)
    context_gate = gate(db_session)
    assessment = context_gate.evaluate_market_context(cell.cell_id, NOW)
    result = context_gate.evaluate_order_intent(intent.intent_id, assessment)
    assert result.counterfactual_opinion == "WOULD_HAVE_AUTHORIZED"
    assert result.veto_reason_code is None


def test_order_evaluation_requires_veto_reason_code_when_vetoing(
    db_session: Session,
) -> None:
    cell, intent = seed_cell_and_intent(db_session)
    assessment = gate(db_session).evaluate_market_context(cell.cell_id, NOW)
    with pytest.raises(DBAPIError), db_session.begin_nested():
        db_session.add(OrderContextEvaluation(
            evaluation_id=uuid4(), intent_id=intent.intent_id,
            assessment_id=assessment.assessment_id,
            counterfactual_opinion="WOULD_HAVE_VETOED",
            veto_reason_code=None, evaluated_at=NOW,
        ))
        db_session.flush()


def test_database_enforces_authority_mode_is_observe_only(
    db_session: Session,
) -> None:
    cell, _ = seed_cell_and_intent(db_session)
    with pytest.raises(DBAPIError), db_session.begin_nested():
        db_session.add(MarketContextAssessment(
            assessment_id=uuid4(), cell_id=cell.cell_id, risk_posture="NORMAL",
            authority_mode="ADVISE", macro_window_active=False,
            assessment_summary="Unauthorized mode.",
            assessment_manifest_sha256="a" * 64,
            evaluated_at=NOW, created_at=NOW,
        ))
        db_session.flush()


def test_context_gate_evaluation_does_not_modify_order_intent_or_risk_governor(
    db_session: Session,
) -> None:
    from engine.intelligence.context import context_gate as module

    source = inspect.getsource(module).lower()
    assert ContextGate.authority_mode == "OBSERVE_ONLY"
    assert "engine.risk" not in source
    assert "engine.execution" not in source
    assert "riskdecision" not in source
    assert "kairoorder" not in source
    cell, intent = seed_cell_and_intent(db_session)
    risk_session = RiskSession(
        session_id=f"context-{uuid4()}", cell_id=cell.cell_id,
        trading_date=date(2026, 9, 1), session_open=NOW - timedelta(hours=1),
        session_close=NOW + timedelta(hours=6), created_at=NOW,
    )
    db_session.add(risk_session)
    db_session.flush()
    state = RiskGovernorState(
        cell_id=cell.cell_id, current_session_id=risk_session.session_id,
        operational_state="ARMED", session_realized_pnl=Decimal("0"),
        session_unrealized_pnl=Decimal("0"), session_fees_usd=Decimal("0"),
        session_slippage_usd=Decimal("0"), session_net_pnl=Decimal("0"),
        last_state_change_at=NOW, updated_at=NOW,
    )
    db_session.add(state)
    db_session.flush()
    intent_before = (intent.target_quantity, intent.order_type, intent.limit_price)
    state_before = (state.operational_state, state.session_net_pnl, state.updated_at)
    context_gate = gate(db_session)
    assessment = context_gate.evaluate_market_context(cell.cell_id, NOW)
    context_gate.evaluate_order_intent(intent.intent_id, assessment)
    assert (intent.target_quantity, intent.order_type, intent.limit_price) == intent_before
    assert (state.operational_state, state.session_net_pnl, state.updated_at) == state_before


def test_context_assessment_manifest_hash_is_deterministic(
    db_session: Session,
) -> None:
    cell, _ = seed_cell_and_intent(db_session)
    context_gate = gate(db_session)
    first = context_gate.evaluate_market_context(cell.cell_id, NOW, "QQQ")
    second = context_gate.evaluate_market_context(cell.cell_id, NOW, "QQQ")
    assert first.assessment_id != second.assessment_id
    assert first.assessment_manifest_sha256 == second.assessment_manifest_sha256


def test_immutability_triggers_reject_update_or_delete_on_context_tables(
    db_session: Session,
) -> None:
    cell, intent = seed_cell_and_intent(db_session)
    context_gate = gate(db_session)
    assessment = context_gate.evaluate_market_context(cell.cell_id, NOW)
    evaluation = context_gate.evaluate_order_intent(intent.intent_id, assessment)
    statements = (
        ("UPDATE market_context_assessments SET risk_posture='ELEVATED' WHERE assessment_id=:id", assessment.assessment_id),
        ("DELETE FROM market_context_assessments WHERE assessment_id=:id", assessment.assessment_id),
        ("UPDATE order_context_evaluations SET counterfactual_opinion='NO_OPINION' WHERE evaluation_id=:id", evaluation.evaluation_id),
        ("DELETE FROM order_context_evaluations WHERE evaluation_id=:id", evaluation.evaluation_id),
    )
    for statement, identity in statements:
        with pytest.raises(DBAPIError, match="immutable"), db_session.begin_nested():
            db_session.execute(text(statement), {"id": identity})


def _alembic_config() -> Config:
    backend_root = Path(__file__).resolve().parents[3]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    return config


def test_migration_0019_upgrade_and_downgrade_are_clean_and_data_safe(
    migrated_database: tuple[str, str],
) -> None:
    admin_url, _ = migrated_database
    config = _alembic_config()
    engine = create_engine(admin_url)
    context_tables = {"market_context_assessments", "order_context_evaluations"}
    cell_id = uuid4()
    try:
        command.downgrade(config, "0018")
        assert context_tables.isdisjoint(sa_inspect(engine).get_table_names())
        assert "effective_at" not in {
            column["name"] for column in sa_inspect(engine).get_columns(
                "intelligence_evidence_ledger"
            )
        }
        command.upgrade(config, "0019")
        assert context_tables <= set(sa_inspect(engine).get_table_names())
        assert "effective_at" in {
            column["name"] for column in sa_inspect(engine).get_columns(
                "intelligence_evidence_ledger"
            )
        }
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO capital_cells
                  (cell_id, cell_code, seed_capital, status, autonomy_tier,
                   strategy_id, strategy_version, target_treasury_code,
                   risk_policy_id, economic_domain)
                VALUES (:cell, :code, 1000, 'ACTIVE', 'APPRENTICE',
                   'EMA-CROSS-001', '1.0.0', 'QQQ', :policy, 'SYNTHETIC')
            """), {"cell": cell_id, "code": f"MIG-{cell_id.hex[:8]}",
                    "policy": DEFAULT_POLICY_ID})
            connection.execute(text("""
                INSERT INTO market_context_assessments
                  (assessment_id, cell_id, risk_posture, authority_mode,
                   macro_window_active, assessment_summary,
                   assessment_manifest_sha256, evaluated_at, created_at)
                VALUES (:assessment, :cell, 'NORMAL', 'OBSERVE_ONLY', false,
                   'Migration safety fact.', :hash, :now, :now)
            """), {"assessment": uuid4(), "cell": cell_id,
                    "hash": "a" * 64, "now": NOW})
        with pytest.raises(Exception, match="immutable context"):
            command.downgrade(config, "0018")
        with engine.begin() as connection:
            connection.execute(text(
                "TRUNCATE order_context_evaluations, market_context_assessments CASCADE"
            ))
            connection.execute(
                text("DELETE FROM capital_cells WHERE cell_id=:cell"),
                {"cell": cell_id},
            )
        command.downgrade(config, "0018")
        command.upgrade(config, "head")
    finally:
        engine.dispose()
