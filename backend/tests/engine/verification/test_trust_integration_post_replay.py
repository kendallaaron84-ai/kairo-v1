from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.configuration import TrustPolicy
from app.db.models.ledger import Fill, TrustEvaluation
from app.domain.enums import AutonomyTier
from engine.research.counterfactual_policy import (
    CounterfactualPolicy,
    PolicyPathPoint,
    PolicySessionPath,
    compare_policies,
)
from engine.trust.evaluator import TrustEvaluator
from engine.trust.models import SafetyEligibility


pytestmark = pytest.mark.integration
POLICY_ID = UUID("50000000-0000-4000-8000-000000000100")
FACTOR_WEIGHTS = {
    "capital_preservation": "0.25",
    "strategy_discipline": "0.20",
    "execution_fidelity": "0.20",
    "context_regime_quality": "0.15",
    "risk_efficiency": "0.10",
    "qualified_capital_production": "0.10",
}


def test_m1_trust_evaluator_persists_manifest_with_insufficient_evidence(
    db_session: Session, verification_replay
) -> None:
    verification_replay.run_entry()
    policy = TrustPolicy(
        policy_id=POLICY_ID,
        version_tag="TRUST-v0.1",
        name="Trust Evaluation Policy v0.1",
        policy_document={
            "factor_weights": FACTOR_WEIGHTS,
            "required_factors": list(FACTOR_WEIGHTS),
            "promotion_thresholds": {"GUARDED": "70", "CAPITAL_BUILDER": "85"},
            "demotion_thresholds": {"GUARDED": "55", "CAPITAL_BUILDER": "70"},
        },
        effective_from=datetime(2026, 9, 1, tzinfo=UTC),
    )
    db_session.add(policy)
    db_session.flush()
    original_tier = verification_replay.cell.autonomy_tier
    evaluator = TrustEvaluator(db_session)
    first = evaluator.evaluate(
        cell_id=verification_replay.cell.cell_id,
        policy_id=policy.policy_id,
        policy_version=policy.version_tag,
        window_size=20,
    )
    second = evaluator.evaluate(
        cell_id=verification_replay.cell.cell_id,
        policy_id=policy.policy_id,
        policy_version=policy.version_tag,
        window_size=20,
    )
    persisted = db_session.get(TrustEvaluation, first.evaluation_id)
    assert persisted is not None
    assert persisted.evidence_manifest_hash == first.evidence_manifest_hash
    assert len(first.evidence_manifest_hash) == 64
    assert second.evidence_manifest_hash == first.evidence_manifest_hash
    assert first.eligibility_status is SafetyEligibility.INSUFFICIENT_EVIDENCE
    assert first.eligible_for_promotion is False
    assert first.score is None
    assert "REQUIRED_SAFETY_AUDIT_UNVERIFIED" in first.disqualifiers
    assert verification_replay.cell.autonomy_tier == original_tier
    assert original_tier == AutonomyTier.APPRENTICE.value
    fills = list(db_session.scalars(select(Fill)))
    assert fills and all(fill.is_simulated for fill in fills)


def test_research_counterfactual_policy_v01_vs_v02_metrics_output(
    db_session: Session, verification_replay
) -> None:
    governor = verification_replay.initialize_governor(
        session_id="COUNTERFACTUAL-READ-ONLY"
    )
    before = _governor_fingerprint(governor.current_state())
    start = datetime(2026, 9, 1, 13, 30, tzinfo=UTC)
    sessions = (
        PolicySessionPath(
            session_id="research-1",
            points=tuple(
                PolicyPathPoint(
                    timestamp=start + timedelta(minutes=index),
                    session_pnl=Decimal(pnl),
                    realized_pnl_delta=Decimal(delta),
                )
                for index, (pnl, delta) in enumerate(
                    (("0", "0"), ("20", "20"), ("30", "10"), ("24", "-6"))
                )
            ),
        ),
        PolicySessionPath(
            session_id="research-2",
            points=(
                PolicyPathPoint(
                    timestamp=start + timedelta(days=1),
                    session_pnl=Decimal("-1"),
                    realized_pnl_delta=Decimal("-1"),
                ),
                PolicyPathPoint(
                    timestamp=start + timedelta(days=1, minutes=1),
                    session_pnl=Decimal("-2"),
                    realized_pnl_delta=Decimal("-1"),
                ),
            ),
        ),
    )
    result = compare_policies(sessions)
    repeated = compare_policies(sessions)
    assert result == repeated
    assert result.baseline_v01.policy is CounterfactualPolicy.V01_PROFIT_CEILING
    assert result.candidate_v02.policy is CounterfactualPolicy.V02_TRAILING_RATCHET
    assert result.baseline_v01.net_realized_profit == Decimal("18")
    assert result.candidate_v02.net_realized_profit == Decimal("22")
    assert result.baseline_v01.max_drawdown == Decimal("1")
    assert result.candidate_v02.max_drawdown == Decimal("6")
    assert result.baseline_v01.peak_profit_capture_ratio == Decimal("20") / Decimal("30")
    assert result.candidate_v02.peak_profit_capture_ratio == Decimal("0.8")
    assert result.baseline_v01.lock_trigger_frequency == Decimal("0.5")
    assert result.candidate_v02.lock_trigger_frequency == Decimal("0.5")
    assert _governor_fingerprint(governor.current_state()) == before


def _governor_fingerprint(state) -> tuple:
    return (
        state.current_session_id,
        state.operational_state,
        state.session_realized_pnl,
        state.session_unrealized_pnl,
        state.session_fees_usd,
        state.session_slippage_usd,
        state.session_net_pnl,
        state.last_state_change_at,
        state.updated_at,
    )
