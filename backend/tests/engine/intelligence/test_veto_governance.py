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

from app.db.models.configuration import Instrument, StrategyRegistry
from app.db.models.intelligence import (
    CellIntelligenceAuthorityEvent,
    IntelligenceAuthorityDecision,
    IntelligenceAuthorityProposal,
    IntelligenceEvidenceLedger,
    IntelligenceRawArtifact,
    IntelligenceResearchRun,
    IntelligenceStatefulReplayRun,
    MarketContextAssessment,
    OrderContextEvaluation,
)
from app.db.models.ledger import CellEvent, KairoOrder, OrderIntent
from app.db.models.projections import CapitalCell
from engine.intelligence.context.context_gate import ContextGate
from engine.intelligence.governance.evaluator import (
    AuthorityGovernanceService,
    PromotionCriteriaEvaluator,
    governance_manifest_sha256,
)
from engine.intelligence.governance.interceptor import RuntimeAuthorityInterceptor
from engine.intelligence.governance.policy import AuthorityPolicyV1


pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 1, 17, 0, tzinfo=UTC)
START = datetime(2026, 6, 1, 13, 30, tzinfo=UTC)
END = datetime(2026, 9, 1, 20, 0, tzinfo=UTC)
POLICY_ID = UUID("a0000000-0000-0000-0000-000000000001")


def seed_cell(session: Session) -> tuple[CapitalCell, Instrument]:
    suffix = uuid4().hex[:10]
    strategy = StrategyRegistry(
        strategy_id=f"VETO-{suffix}", version_tag="1.0.0",
        display_name="Step 6 governance fixture", status="ACTIVE",
        configuration={"clearance": "PAPER_ONLY"},
    )
    instrument = Instrument(
        instrument_id=uuid4(), symbol=f"V{suffix[:8]}", asset_class="EQUITY",
        currency="USD", effective_from=START,
    )
    session.add_all([strategy, instrument])
    session.flush()
    cell = CapitalCell(
        cell_id=uuid4(), cell_code=f"VETO-{suffix}", seed_capital=Decimal("200.00"),
        status="ACTIVE", autonomy_tier="APPRENTICE", strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag, target_treasury_code=instrument.symbol,
        risk_policy_id=POLICY_ID, economic_domain="SYNTHETIC", updated_at=START,
    )
    session.add(cell)
    session.flush()
    return cell, instrument


def add_veto_evaluations(
    session: Session, cell: CapitalCell, instrument: Instrument, count: int, months: int,
) -> None:
    for index in range(count):
        month_offset = index % months
        evaluated_at = START + timedelta(days=31 * month_offset, minutes=index)
        intent = OrderIntent(
            intent_id=uuid4(), cell_id=cell.cell_id, strategy_id=cell.strategy_id,
            strategy_version=cell.strategy_version, instrument_id=instrument.instrument_id,
            client_order_key=f"veto-evidence-{uuid4()}", order_purpose="ENTRY",
            side="BUY", target_quantity=Decimal("1"), order_type="MARKET",
            created_at=evaluated_at,
        )
        session.add(intent)
        session.flush()
        assessment_id = uuid4()
        session.add(MarketContextAssessment(
            assessment_id=assessment_id, cell_id=cell.cell_id,
            risk_posture="HIGH_EVENT_RISK", authority_mode="OBSERVE_ONLY",
            macro_window_active=False, assessment_summary="Historical policy evidence.",
            assessment_manifest_sha256=hashlib.sha256(str(assessment_id).encode()).hexdigest(),
            evaluated_at=evaluated_at, created_at=evaluated_at,
        ))
        session.flush()
        session.execute(text("""
            INSERT INTO order_context_evaluations
              (evaluation_id, intent_id, assessment_id, counterfactual_opinion,
               veto_reason_code, evaluated_at)
            VALUES (:id, :intent, :assessment, 'WOULD_HAVE_VETOED',
                    'CRITICAL_MACRO_WINDOW_ACTIVE', :at)
        """), {"id": uuid4(), "intent": intent.intent_id,
                 "assessment": assessment_id, "at": evaluated_at})
    session.flush()


def add_research_pair(
    session: Session, cell: CapitalCell, *, vetoes: int = 30,
    start: datetime = START, end: datetime = END,
) -> tuple[IntelligenceResearchRun, IntelligenceStatefulReplayRun]:
    run5 = IntelligenceResearchRun(
        run_id=uuid4(), cell_id=cell.cell_id,
        research_method="TRADE_REMOVAL_COUNTERFACTUAL",
        sample_start_time=start, sample_end_time=end,
        total_baseline_trades=vetoes, total_context_evaluated_trades=vetoes,
        total_veto_opportunities=vetoes, vetoed_losing_trades=(vetoes * 7) // 10,
        vetoed_winning_trades=vetoes - ((vetoes * 7) // 10),
        vetoed_breakeven_trades=0, excluded_causal_invalid_trades=0,
        baseline_net_pnl=Decimal("0.00"), counterfactual_net_pnl=Decimal("12.00"),
        losses_avoided_usd=Decimal("21.00"), profits_forfeited_usd=Decimal("9.00"),
        net_alpha_usd=Decimal("12.00"), baseline_max_drawdown_usd=Decimal("20.00"),
        counterfactual_max_drawdown_usd=Decimal("10.00"), veto_precision_pct=Decimal("70.00"),
        research_manifest_sha256="5" * 64, executed_at=NOW,
    )
    run55 = IntelligenceStatefulReplayRun(
        replay_run_id=uuid4(), cell_id=cell.cell_id,
        research_method="STATEFUL_REPLAY_COUNTERFACTUAL",
        sample_start_time=start, sample_end_time=end,
        baseline_trade_count=vetoes, counterfactual_trade_count=vetoes,
        direct_vetoed_trades_count=vetoes, induced_trades_taken_count=0,
        induced_trades_missed_count=0, baseline_net_pnl=Decimal("0.00"),
        counterfactual_net_pnl=Decimal("8.00"), stateful_net_alpha_usd=Decimal("8.00"),
        baseline_max_drawdown_usd=Decimal("20.00"),
        counterfactual_max_drawdown_usd=Decimal("12.00"),
        drawdown_reduction_usd=Decimal("8.00"), baseline_halt_count=1,
        counterfactual_halt_count=0, siphon_delta_treasury_usd=Decimal("0.00"),
        siphon_delta_replication_usd=Decimal("0.00"),
        siphon_delta_safety_usd=Decimal("0.00"), baseline_cell_count=1,
        counterfactual_cell_count=1, stateful_replay_manifest_sha256="6" * 64,
        executed_at=NOW,
    )
    session.add_all([run5, run55])
    session.flush()
    return run5, run55


def proposal_fixture(
    session: Session, *, vetoes: int = 30, months: int = 3,
) -> tuple[CapitalCell, Instrument, IntelligenceAuthorityProposal]:
    cell, instrument = seed_cell(session)
    add_veto_evaluations(session, cell, instrument, vetoes, months)
    run5, run55 = add_research_pair(session, cell, vetoes=vetoes)
    proposal = PromotionCriteriaEvaluator(session, clock=lambda: NOW).evaluate(
        cell_id=cell.cell_id, step5_run_id=run5.run_id,
        step5_5_run_id=run55.replay_run_id,
    )
    return cell, instrument, proposal


def grant_fixture(
    session: Session,
) -> tuple[CapitalCell, Instrument, CellIntelligenceAuthorityEvent]:
    cell, instrument, proposal = proposal_fixture(session)
    service = AuthorityGovernanceService(session, clock=lambda: NOW + timedelta(minutes=1))
    decision = service.decide(proposal.proposal_id, "APPROVED", "human@example.com")
    event = service.record_event(
        cell_id=cell.cell_id, event_type="GRANTED",
        operator_identity="human@example.com", decision_id=decision.decision_id,
        effective_at=NOW + timedelta(minutes=2),
    )
    return cell, instrument, event


def add_runtime_context(
    session: Session, cell: CapitalCell, instrument: Instrument, at: datetime,
    *, title: str = "FOMC policy decision", reason_code: str | None = None,
) -> OrderIntent:
    artifact_id = uuid4()
    digest = hashlib.sha256(str(artifact_id).encode()).hexdigest()
    session.add(IntelligenceRawArtifact(
        artifact_id=artifact_id, content_sha256=digest, mime_type="text/plain",
        byte_size=1, storage_uri=f"sha256/{digest}", created_at=at,
    ))
    session.flush()
    event = IntelligenceEvidenceLedger(
        event_id=uuid4(), artifact_id=artifact_id, source_type="PRIMARY",
        source_name="FEDERAL_RESERVE", source_uri="https://federalreserve.gov/test",
        event_type="MACRO", title=title, summary="Official critical macro event.",
        published_at=at, observed_at=at - timedelta(minutes=1), impact_scope="MARKET",
        urgency="CRITICAL", confidence_score=Decimal("100.00"), time_horizon="INTRADAY",
        raw_content_sha256=digest, release_status="SCHEDULED", effective_at=at,
        created_at=at,
    )
    session.add(event)
    session.flush()
    intent = OrderIntent(
        intent_id=uuid4(), cell_id=cell.cell_id, strategy_id=cell.strategy_id,
        strategy_version=cell.strategy_version, instrument_id=instrument.instrument_id,
        client_order_key=f"runtime-veto-{uuid4()}", order_purpose="ENTRY", side="BUY",
        target_quantity=Decimal("1"), order_type="LIMIT", limit_price=Decimal("1.00"),
        created_at=at,
    )
    session.add(intent)
    session.flush()
    gate = ContextGate(session, clock=lambda: at)
    assessment = gate.evaluate_market_context(cell.cell_id, at)
    if reason_code is None:
        gate.evaluate_order_intent(intent.intent_id, assessment)
    else:
        session.add(OrderContextEvaluation(
            evaluation_id=uuid4(), intent_id=intent.intent_id,
            assessment_id=assessment.assessment_id,
            counterfactual_opinion="WOULD_HAVE_VETOED",
            veto_reason_code=reason_code, evaluated_at=at,
        ))
        session.flush()
    return intent


def test_promotion_rejected_when_vetoes_do_not_span_three_independent_months(db_session: Session) -> None:
    _, _, proposal = proposal_fixture(db_session, months=2)
    assert proposal.distinct_trading_months == 2
    assert proposal.criteria_passed is False


def test_promotion_rejected_when_veto_opportunities_below_thirty(db_session: Session) -> None:
    _, _, proposal = proposal_fixture(db_session, vetoes=29, months=3)
    assert proposal.evaluated_veto_opportunities == 29
    assert proposal.criteria_passed is False


def test_step5_and_step5_5_evidence_must_reference_compatible_cell_and_period(db_session: Session) -> None:
    cell, instrument = seed_cell(db_session)
    add_veto_evaluations(db_session, cell, instrument, 30, 3)
    run5, _ = add_research_pair(db_session, cell)
    other, _ = seed_cell(db_session)
    _, other55 = add_research_pair(db_session, other)
    with pytest.raises(ValueError, match="same cell"):
        PromotionCriteriaEvaluator(db_session).evaluate(
            cell_id=cell.cell_id, step5_run_id=run5.run_id,
            step5_5_run_id=other55.replay_run_id,
        )


def test_database_rejects_human_decision_with_nonmatching_proposal_manifest_hash(db_session: Session) -> None:
    _, _, proposal = proposal_fixture(db_session)
    with pytest.raises(DBAPIError, match="manifest hash mismatch"), db_session.begin_nested():
        db_session.add(IntelligenceAuthorityDecision(
            decision_id=uuid4(), proposal_id=proposal.proposal_id, decision="APPROVED",
            operator_identity="human@example.com", approved_proposal_manifest_sha256="0" * 64,
            decision_manifest_sha256="1" * 64, decided_at=NOW + timedelta(minutes=1),
        ))
        db_session.flush()


def test_authority_can_be_revoked_append_only_without_rewriting_grant_history(db_session: Session) -> None:
    cell, _, grant = grant_fixture(db_session)
    service = AuthorityGovernanceService(db_session, clock=lambda: NOW + timedelta(minutes=3))
    service.record_event(cell_id=cell.cell_id, event_type="REVOKED", operator_identity="human@example.com")
    events = list(db_session.scalars(select(CellIntelligenceAuthorityEvent).where(
        CellIntelligenceAuthorityEvent.cell_id == cell.cell_id
    ).order_by(CellIntelligenceAuthorityEvent.effective_at)))
    assert [(row.event_type, row.authority_mode) for row in events] == [
        ("GRANTED", "VETO_ONLY"), ("REVOKED", "OBSERVE_ONLY")
    ]
    assert events[0].event_id == grant.event_id


def test_market_context_assessments_remain_observe_only_after_veto_authority_grant(db_session: Session) -> None:
    cell, instrument, _ = grant_fixture(db_session)
    add_runtime_context(db_session, cell, instrument, NOW + timedelta(minutes=3))
    assert set(db_session.scalars(select(MarketContextAssessment.authority_mode))) == {"OBSERVE_ONLY"}


def test_veto_grant_is_bound_to_exact_authority_policy_version(db_session: Session) -> None:
    _, _, event = grant_fixture(db_session)
    assert event.policy_version == AuthorityPolicyV1.POLICY_VERSION
    with pytest.raises(DBAPIError), db_session.begin_nested():
        db_session.execute(text("UPDATE cell_intelligence_authority_events SET policy_version='v2' WHERE event_id=:id"), {"id": event.event_id})


def test_veto_only_policy_cannot_expand_to_new_reason_code_without_new_authorization(db_session: Session) -> None:
    cell, instrument, _ = grant_fixture(db_session)
    intent = add_runtime_context(
        db_session, cell, instrument, NOW + timedelta(minutes=3),
        reason_code="UNAUTHORIZED_REASON",
    )
    result = RuntimeAuthorityInterceptor(db_session).intercept(intent.intent_id, evaluated_at=NOW + timedelta(minutes=3))
    assert result.route_to_broker is True


def test_no_cell_receives_veto_authority_merely_from_migration_or_criteria_pass(db_session: Session) -> None:
    cell, _, proposal = proposal_fixture(db_session)
    assert proposal.criteria_passed is True
    assert db_session.scalar(select(func.count()).select_from(CellIntelligenceAuthorityEvent)) == 0
    result = RuntimeAuthorityInterceptor(db_session).intercept(
        db_session.scalar(select(OrderIntent.intent_id).where(OrderIntent.cell_id == cell.cell_id).limit(1)),
        evaluated_at=NOW,
    )
    assert result.authority_mode == "OBSERVE_ONLY"


def test_veto_only_cell_suppresses_trade_intent_during_critical_macro_window(db_session: Session) -> None:
    cell, instrument, _ = grant_fixture(db_session)
    at = NOW + timedelta(minutes=3)
    intent = add_runtime_context(db_session, cell, instrument, at)
    result = RuntimeAuthorityInterceptor(db_session).intercept(intent.intent_id, evaluated_at=at)
    assert result.route_to_broker is False
    fact = db_session.get(CellEvent, result.veto_fact_id)
    assert fact.event_type == "INTENT_VETOED"
    assert fact.payload["runtime_effect"] == "SUPPRESS_ONLY"
    assert db_session.scalar(select(func.count()).select_from(KairoOrder).where(KairoOrder.intent_id == intent.intent_id)) == 0


def test_veto_only_cannot_originate_or_resize_trade_intents(db_session: Session) -> None:
    cell, instrument, _ = grant_fixture(db_session)
    at = NOW + timedelta(minutes=3)
    intent = add_runtime_context(db_session, cell, instrument, at)
    before = (intent.target_quantity, intent.limit_price, intent.side)
    RuntimeAuthorityInterceptor(db_session).intercept(intent.intent_id, evaluated_at=at)
    db_session.refresh(intent)
    assert (intent.target_quantity, intent.limit_price, intent.side) == before
    source = inspect.getsource(RuntimeAuthorityInterceptor.intercept)
    assert "OrderIntent(" not in source and "KairoOrder(" not in source


def test_observe_only_sibling_cell_unaffected_by_veto_only_grant(db_session: Session) -> None:
    grant_fixture(db_session)
    sibling, instrument = seed_cell(db_session)
    at = NOW + timedelta(minutes=3)
    intent = add_runtime_context(db_session, sibling, instrument, at)
    result = RuntimeAuthorityInterceptor(db_session).intercept(intent.intent_id, evaluated_at=at)
    assert result.route_to_broker is True
    assert result.authority_mode == "OBSERVE_ONLY"


def test_authority_proposals_and_decisions_are_immutable(db_session: Session) -> None:
    _, _, proposal = proposal_fixture(db_session)
    decision = AuthorityGovernanceService(db_session, clock=lambda: NOW + timedelta(minutes=1)).decide(
        proposal.proposal_id, "REJECTED", "human@example.com"
    )
    for table, key, value in (
        ("intelligence_authority_proposals", "proposal_id", proposal.proposal_id),
        ("intelligence_authority_decisions", "decision_id", decision.decision_id),
    ):
        with pytest.raises(DBAPIError, match="immutable"), db_session.begin_nested():
            db_session.execute(text(f"DELETE FROM {table} WHERE {key}=:id"), {"id": value})


def test_database_blocks_authority_grant_when_criteria_failed(db_session: Session) -> None:
    cell, _, proposal = proposal_fixture(db_session, vetoes=29, months=3)
    service = AuthorityGovernanceService(db_session, clock=lambda: NOW + timedelta(minutes=1))
    decision = service.decide(proposal.proposal_id, "APPROVED", "human@example.com")
    with pytest.raises(DBAPIError, match="criteria failed"), db_session.begin_nested():
        service.record_event(
            cell_id=cell.cell_id, event_type="GRANTED", operator_identity="human@example.com",
            decision_id=decision.decision_id, effective_at=NOW + timedelta(minutes=2),
        )


def test_migration_0022_upgrade_and_downgrade_are_clean_and_data_safe(
    migrated_database: tuple[str, str],
) -> None:
    admin_url, _ = migrated_database
    backend_root = Path(__file__).resolve().parents[3]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    command.downgrade(config, "0021")
    engine = create_engine(admin_url)
    try:
        names = set(sa_inspect(engine).get_table_names())
        assert "intelligence_authority_proposals" not in names
        command.upgrade(config, "0022")
        names = set(sa_inspect(engine).get_table_names())
        assert {"intelligence_authority_proposals", "intelligence_authority_decisions", "cell_intelligence_authority_events"} <= names
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM cell_intelligence_authority_events")) == 0
        command.downgrade(config, "0021")
        command.upgrade(config, "head")
    finally:
        engine.dispose()
