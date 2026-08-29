from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.models.configuration import StrategyRegistry, TrustPolicy
from app.db.models.ledger import TrustEvaluation
from app.db.models.projections import CapitalCell
from app.domain.enums import OrderSide
from engine.trust.evaluator import TrustEvaluator
from engine.trust.models import (
    ClosedTradeEvidence,
    EquityPoint,
    EvidenceStatus,
    ExecutionEvidence,
    GovernorAuditEvidence,
    SafetyAuditEvidence,
    SafetyEligibility,
    TrustEvidenceBundle,
)
from engine.trust.scoring_factors import (
    adverse_slippage_usd,
    chronological_drawdown,
)


pytestmark = pytest.mark.integration


FACTOR_WEIGHTS = {
    "risk_adjusted_outcomes": "0.20",
    "drawdown_control": "0.15",
    "execution_quality": "0.15",
    "excursion_efficiency": "0.15",
    "strategy_discipline": "0.20",
    "regime_consistency": "0.15",
}


def seed_policy_and_cell(session: Session, *, status: str = "APPRENTICE"):
    strategy = StrategyRegistry(
        strategy_id=f"TRUST-{uuid4().hex[:8]}",
        version_tag="1.0.0",
        display_name="Trust test strategy",
        status="ACTIVE",
        configuration={},
    )
    policy = TrustPolicy(
        policy_id=uuid4(),
        version_tag="TRUST-v0.1",
        name="Trust Evaluation Policy v0.1",
        policy_document={
            "factor_weights": FACTOR_WEIGHTS,
            "required_factors": list(FACTOR_WEIGHTS),
            "promotion_thresholds": {"GUARDED": "70", "AUTONOMOUS": "85"},
            "demotion_thresholds": {"GUARDED": "55", "AUTONOMOUS": "70"},
        },
    )
    session.add_all([strategy, policy])
    session.flush()
    cell = CapitalCell(
        cell_id=uuid4(),
        cell_code=f"TRUST-CELL-{uuid4().hex[:8]}",
        seed_capital=Decimal("100"),
        status=status,
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag,
        target_treasury_code="META",
    )
    session.add(cell)
    session.flush()
    return cell, policy


def complete_evidence(cell_id, count: int = 20, *, realized: str = "1"):
    start = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
    trades = tuple(
        ClosedTradeEvidence(
            trade_id=uuid4(),
            closed_at=start + timedelta(minutes=index),
            realized_pnl_usd=Decimal(realized),
            planned_risk_usd=Decimal("1"),
            mfe_r=Decimal("2"),
            mae_r=Decimal("1"),
            regime="TREND",
            strategy_compliant=True,
            settlement_verified=True,
        )
        for index in range(count)
    )
    equity = tuple(
        EquityPoint(
            timestamp=trade.closed_at,
            equity=Decimal("100") + Decimal(index),
        )
        for index, trade in enumerate(trades)
    )
    executions = tuple(
        ExecutionEvidence(
            fill_id=uuid4(),
            filled_at=trade.closed_at,
            side=OrderSide.BUY,
            fill_price=Decimal("10"),
            reference_price=Decimal("10"),
            quantity=Decimal("1"),
            contract_multiplier=Decimal("1"),
        )
        for trade in trades
    )
    return TrustEvidenceBundle(
        cell_id=cell_id,
        closed_trades=trades,
        equity_curve=equity,
        executions=executions,
        safety=SafetyAuditEvidence(
            broker_reconciliation_verified=True,
            post_halt_trading_verified_clean=True,
            parameter_controls_verified_clean=True,
        ),
        governor=GovernorAuditEvidence(authorized_intents=count),
    )


def factor(result, name: str):
    return next(item for item in result.factors if item.factor == name)


def test_insufficient_safety_evidence_blocks_promotion(db_session: Session) -> None:
    cell, policy = seed_policy_and_cell(db_session)
    evidence = complete_evidence(cell.cell_id).model_copy(
        update={
            "safety": SafetyAuditEvidence(
                broker_reconciliation_verified=None,
                post_halt_trading_verified_clean=True,
                parameter_controls_verified_clean=True,
            )
        }
    )
    result = TrustEvaluator(db_session).evaluate(
        cell_id=cell.cell_id,
        policy_id=policy.policy_id,
        policy_version=policy.version_tag,
        window_size=20,
        evidence=evidence,
    )
    assert result.eligibility_status is SafetyEligibility.INSUFFICIENT_EVIDENCE
    assert result.eligible_for_promotion is False
    assert result.recommended_autonomy_tier == "APPRENTICE"


def test_missing_factor_evidence_blocks_promotion_without_score_fabrication(
    db_session: Session,
) -> None:
    cell, policy = seed_policy_and_cell(db_session)
    evidence = complete_evidence(cell.cell_id)
    trades = list(evidence.closed_trades)
    trades[0] = trades[0].model_copy(update={"mfe_r": None})
    result = TrustEvaluator(db_session).evaluate(
        cell_id=cell.cell_id,
        policy_id=policy.policy_id,
        policy_version=policy.version_tag,
        window_size=20,
        evidence=evidence.model_copy(update={"closed_trades": tuple(trades)}),
    )
    efficiency = factor(result, "excursion_efficiency")
    assert efficiency.status is EvidenceStatus.INSUFFICIENT_EVIDENCE
    assert efficiency.score is None
    assert result.score is None
    assert result.eligible_for_promotion is False


def test_undefined_efficiency_returns_insufficient_evidence(
    db_session: Session,
) -> None:
    cell, policy = seed_policy_and_cell(db_session)
    evidence = complete_evidence(cell.cell_id)
    trades = tuple(
        trade.model_copy(update={"mfe_r": Decimal("0"), "mae_r": Decimal("0")})
        for trade in evidence.closed_trades
    )
    result = TrustEvaluator(db_session).evaluate(
        cell_id=cell.cell_id,
        policy_id=policy.policy_id,
        policy_version=policy.version_tag,
        window_size=20,
        evidence=evidence.model_copy(update={"closed_trades": trades}),
    )
    efficiency = factor(result, "excursion_efficiency")
    assert efficiency.status is EvidenceStatus.INSUFFICIENT_EVIDENCE
    assert efficiency.score is None
    assert result.score is None


def test_rejected_intents_penalize_discipline_without_hard_disqualification(
    db_session: Session,
) -> None:
    cell, policy = seed_policy_and_cell(db_session)
    clean_evidence = complete_evidence(cell.cell_id)
    evaluator = TrustEvaluator(db_session)
    clean = evaluator.evaluate(
        cell_id=cell.cell_id,
        policy_id=policy.policy_id,
        policy_version=policy.version_tag,
        window_size=20,
        evidence=clean_evidence,
    )
    penalized = evaluator.evaluate(
        cell_id=cell.cell_id,
        policy_id=policy.policy_id,
        policy_version=policy.version_tag,
        window_size=20,
        evidence=clean_evidence.model_copy(
            update={"governor": GovernorAuditEvidence(authorized_intents=20, rejected_intents=10)}
        ),
    )
    assert penalized.eligibility_status is SafetyEligibility.ELIGIBLE
    assert factor(penalized, "strategy_discipline").score < factor(
        clean, "strategy_discipline"
    ).score


def test_actual_safety_bypass_triggers_disqualification(db_session: Session) -> None:
    cell, policy = seed_policy_and_cell(db_session)
    evidence = complete_evidence(cell.cell_id).model_copy(
        update={
            "safety": SafetyAuditEvidence(
                broker_reconciliation_verified=True,
                post_halt_trading_verified_clean=True,
                parameter_controls_verified_clean=True,
                parameter_bypass_detected=True,
            )
        }
    )
    result = TrustEvaluator(db_session).evaluate(
        cell_id=cell.cell_id,
        policy_id=policy.policy_id,
        policy_version=policy.version_tag,
        window_size=20,
        evidence=evidence,
    )
    assert result.eligibility_status is SafetyEligibility.DISQUALIFIED
    assert result.eligible_for_promotion is False
    assert "PARAMETER_BYPASS" in result.disqualifiers


def test_chronological_drawdown_calculation() -> None:
    now = datetime.now(UTC)
    points = [
        EquityPoint(timestamp=now, equity=Decimal("100")),
        EquityPoint(timestamp=now + timedelta(minutes=1), equity=Decimal("120")),
        EquityPoint(timestamp=now + timedelta(minutes=2), equity=Decimal("90")),
        EquityPoint(timestamp=now + timedelta(minutes=3), equity=Decimal("130")),
    ]
    result = chronological_drawdown(points)
    assert result.amount == Decimal("30")
    assert result.percent == Decimal("0.25")


def test_side_aware_option_adverse_slippage() -> None:
    buy = ExecutionEvidence(
        fill_id=uuid4(),
        side=OrderSide.BUY,
        fill_price=Decimal("1.20"),
        reference_price=Decimal("1.00"),
        quantity=Decimal("2"),
        contract_multiplier=Decimal("10"),
    )
    sell = buy.model_copy(
        update={
            "fill_id": uuid4(),
            "side": OrderSide.SELL,
            "fill_price": Decimal("0.80"),
        }
    )
    assert adverse_slippage_usd(buy) == Decimal("4.00")
    assert adverse_slippage_usd(sell) == Decimal("4.00")


def test_rolling_window_recomputes_over_n_trades(db_session: Session) -> None:
    cell, policy = seed_policy_and_cell(db_session)
    evaluator = TrustEvaluator(db_session)
    losing = complete_evidence(cell.cell_id, realized="-1")
    prior = evaluator.evaluate(
        cell_id=cell.cell_id,
        policy_id=policy.policy_id,
        policy_version=policy.version_tag,
        window_size=20,
        evidence=losing,
    )
    winning = complete_evidence(cell.cell_id, realized="1")
    combined = winning.model_copy(
        update={"closed_trades": losing.closed_trades + winning.closed_trades}
    )
    current = evaluator.evaluate(
        cell_id=cell.cell_id,
        policy_id=policy.policy_id,
        policy_version=policy.version_tag,
        window_size=20,
        evidence=combined,
    )
    assert prior.score != current.score
    assert factor(current, "risk_adjusted_outcomes").score == Decimal("75")
    assert current.window_trade_count == 20


def test_evaluator_emits_recommendation_without_mutating_cell_tier(
    db_session: Session,
) -> None:
    cell, policy = seed_policy_and_cell(db_session)
    original_status = cell.status
    result = TrustEvaluator(db_session).evaluate(
        cell_id=cell.cell_id,
        policy_id=policy.policy_id,
        policy_version=policy.version_tag,
        window_size=20,
        evidence=complete_evidence(cell.cell_id),
    )
    db_session.refresh(cell)
    assert result.recommended_autonomy_tier == "GUARDED"
    assert cell.status == original_status == "APPRENTICE"


def test_w50_recommends_autonomous_without_mutating_guarded_cell(
    db_session: Session,
) -> None:
    cell, policy = seed_policy_and_cell(db_session, status="GUARDED")
    result = TrustEvaluator(db_session).evaluate(
        cell_id=cell.cell_id,
        policy_id=policy.policy_id,
        policy_version=policy.version_tag,
        window_size=50,
        evidence=complete_evidence(cell.cell_id, count=50),
    )
    db_session.refresh(cell)
    assert result.recommended_autonomy_tier == "AUTONOMOUS"
    assert cell.status == "GUARDED"


def test_immutable_evaluation_persistence_with_manifest_hash(
    db_session: Session,
) -> None:
    cell, policy = seed_policy_and_cell(db_session)
    result = TrustEvaluator(db_session).evaluate(
        cell_id=cell.cell_id,
        policy_id=policy.policy_id,
        policy_version=policy.version_tag,
        window_size=20,
        evidence=complete_evidence(cell.cell_id),
    )
    persisted = db_session.get(TrustEvaluation, result.evaluation_id)
    assert persisted.cell_id == cell.cell_id
    assert persisted.policy_id == policy.policy_id
    assert persisted.policy_version == policy.version_tag
    assert persisted.evidence_manifest_hash == result.evidence_manifest_hash
    assert len(persisted.evidence_manifest_hash) == 64
    assert persisted.window_trade_count == 20


def test_runtime_role_cannot_mutate_trust_evaluations(
    migrated_database: tuple[str, str],
) -> None:
    admin_url, _ = migrated_database
    engine = create_engine(admin_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT has_table_privilege("
                    "'kairo_runtime', 'trust_evaluations', 'SELECT,INSERT')"
                )
            ) is True
            assert connection.scalar(
                text(
                    "SELECT has_table_privilege("
                    "'kairo_runtime', 'trust_evaluations', 'UPDATE,DELETE')"
                )
            ) is False
    finally:
        engine.dispose()
