from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.db.models.configuration import TrustPolicy
from app.db.models.ledger import TrustEvaluation
from app.db.models.projections import CapitalCell
from app.domain.enums import TrustOutcome
from engine.trust.evidence_collector import TrustEvidenceCollector
from engine.trust.hysteresis import TIER_ORDER, latest_window, recommend_autonomy_tier
from engine.trust.manifest import evidence_manifest_hash
from engine.trust.models import (
    SafetyAuditEvidence,
    SafetyEligibility,
    TrustEvaluationResult,
    TrustEvidenceBundle,
    TrustPolicySpec,
)
from engine.trust.scoring_factors import (
    TRUST_V01_FACTORS,
    TRUST_V01_WEIGHTS,
    compute_factor_scores,
    weighted_score,
)


class TrustPolicyError(ValueError):
    pass


class TrustEvaluator:
    def __init__(self, session: Session):
        self.session = session
        self.collector = TrustEvidenceCollector(session)

    def evaluate(
        self,
        *,
        cell_id: UUID,
        policy_id: UUID,
        policy_version: str,
        window_size: int,
        evidence: TrustEvidenceBundle | None = None,
    ) -> TrustEvaluationResult:
        cell = self.session.get(CapitalCell, cell_id)
        if cell is None:
            raise TrustPolicyError("canonical capital cell does not exist")
        policy = self.session.get(TrustPolicy, (policy_id, policy_version))
        if policy is None or policy.retired_at is not None:
            raise TrustPolicyError("active trust policy version does not exist")
        spec = self._policy_spec(policy.policy_document)
        collected = evidence or self.collector.collect(cell_id, window_size)
        if collected.cell_id != cell_id:
            raise TrustPolicyError("evidence cell identity does not match canonical cell")
        window = self._window(collected, window_size)

        eligibility, disqualifiers = self._safety_eligibility(window.safety)
        factors = compute_factor_scores(window, spec.factor_weights)
        score = weighted_score(factors, spec.required_factors)
        recommended = recommend_autonomy_tier(
            current_tier=cell.autonomy_tier,
            eligibility=eligibility,
            score=score,
            trade_count=len(window.closed_trades),
            window_size=window_size,
            promotion_thresholds=spec.promotion_thresholds,
            demotion_thresholds=spec.demotion_thresholds,
        )
        current_index = (
            TIER_ORDER.index(cell.autonomy_tier)
            if cell.autonomy_tier in TIER_ORDER
            else 0
        )
        recommended_index = TIER_ORDER.index(recommended)
        eligible_for_promotion = (
            eligibility is SafetyEligibility.ELIGIBLE
            and score is not None
            and len(window.closed_trades) >= window_size
            and recommended_index > current_index
        )
        window_start = window.closed_trades[0].closed_at if window.closed_trades else None
        window_end = window.closed_trades[-1].closed_at if window.closed_trades else None
        evaluation_id = uuid4()
        manifest = evidence_manifest_hash(
            {
                "policy_id": policy_id,
                "policy_version": policy_version,
                "policy_document": policy.policy_document,
                "window_size": window_size,
                "evidence": window,
                "factors": factors,
                "eligibility": eligibility,
            }
        )
        outcome = (
            TrustOutcome.DEMOTE
            if eligibility is SafetyEligibility.DISQUALIFIED
            else TrustOutcome.PASS
            if eligibility is SafetyEligibility.ELIGIBLE and score is not None
            else TrustOutcome.FAIL
        )
        self.session.add(
            TrustEvaluation(
                evaluation_id=evaluation_id,
                cell_id=cell_id,
                policy_id=policy_id,
                policy_version=policy_version,
                score=score,
                outcome=outcome.value,
                eligible_for_promotion=eligible_for_promotion,
                evidence_trade_count=len(window.closed_trades),
                window_trade_count=len(window.closed_trades),
                window_start=window_start,
                window_end=window_end,
                evidence_manifest_hash=manifest,
                eligibility_status=eligibility.value,
                current_autonomy_tier=cell.autonomy_tier,
                recommended_autonomy_tier=recommended,
                disqualifiers=list(disqualifiers),
                factor_breakdown={
                    factor.factor: factor.model_dump(mode="json") for factor in factors
                },
                details={
                    "window": f"W_{window_size}",
                    "required_factors": list(spec.required_factors),
                    "score_math": "available factor weights renormalized to 1.0",
                },
            )
        )
        self.session.flush()
        return TrustEvaluationResult(
            evaluation_id=evaluation_id,
            cell_id=cell_id,
            policy_id=policy_id,
            policy_version=policy_version,
            eligibility_status=eligibility,
            score=score,
            eligible_for_promotion=eligible_for_promotion,
            current_autonomy_tier=cell.autonomy_tier,
            recommended_autonomy_tier=recommended,
            evidence_trade_count=len(window.closed_trades),
            window_trade_count=len(window.closed_trades),
            window_start=window_start,
            window_end=window_end,
            factors=factors,
            disqualifiers=disqualifiers,
            evidence_manifest_hash=manifest,
        )

    @staticmethod
    def _policy_spec(document: dict) -> TrustPolicySpec:
        try:
            spec = TrustPolicySpec.model_validate(document)
        except Exception as exc:
            raise TrustPolicyError("invalid TRUST-v0.1 policy document") from exc
        expected = set(TRUST_V01_FACTORS)
        if set(spec.factor_weights) != expected or set(spec.required_factors) != expected:
            raise TrustPolicyError("TRUST-v0.1 requires exactly all six canonical factors")
        if spec.factor_weights != TRUST_V01_WEIGHTS:
            raise TrustPolicyError("TRUST-v0.1 factor weights must match frozen policy")
        return spec

    @staticmethod
    def _window(evidence: TrustEvidenceBundle, window_size: int) -> TrustEvidenceBundle:
        trades = latest_window(evidence.closed_trades, window_size)
        if not trades:
            return evidence.model_copy(update={"closed_trades": trades})
        start = trades[0].closed_at
        end = trades[-1].closed_at
        equity = tuple(
            point for point in evidence.equity_curve if start <= point.timestamp <= end
        )
        executions = tuple(
            item
            for item in evidence.executions
            if item.filled_at is None or start <= item.filled_at <= end
        )
        return evidence.model_copy(
            update={
                "closed_trades": trades,
                "equity_curve": equity,
                "executions": executions,
            }
        )

    @staticmethod
    def _safety_eligibility(
        safety: SafetyAuditEvidence,
    ) -> tuple[SafetyEligibility, tuple[str, ...]]:
        bypasses = []
        if safety.unauthorized_execution_detected:
            bypasses.append("UNAUTHORIZED_EXECUTION")
        if safety.post_halt_execution_detected:
            bypasses.append("POST_HALT_EXECUTION")
        if safety.parameter_bypass_detected:
            bypasses.append("PARAMETER_BYPASS")
        if bypasses:
            return SafetyEligibility.DISQUALIFIED, tuple(bypasses)
        required = (
            safety.broker_reconciliation_verified,
            safety.post_halt_trading_verified_clean,
            safety.parameter_controls_verified_clean,
        )
        if not all(value is True for value in required):
            return SafetyEligibility.INSUFFICIENT_EVIDENCE, (
                "REQUIRED_SAFETY_AUDIT_UNVERIFIED",
            )
        return SafetyEligibility.ELIGIBLE, ()
