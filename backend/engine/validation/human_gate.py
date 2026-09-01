import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.scorecards import (
    HistoricalValidationAcceptanceFact,
    HistoricalValidationConfidenceLedger,
    HistoricalValidationRun,
)


AcceptanceDecision = Literal["ACCEPTED_FOR_LIVE", "REJECTED", "CONDITIONAL_REVIEW"]


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


def decision_manifest_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(_canonical(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ValidationReviewPackage(BaseModel):
    model_config = ConfigDict(frozen=True)

    validation_run_id: UUID
    confidence_ledger_id: UUID
    confidence_policy_version: str
    confidence_score: Decimal
    confidence_tier: str
    hard_gate_passed: bool
    gate_eligible: bool
    hard_gate_evaluations: dict[str, Any]
    confidence_manifest_sha256: str
    scorecard_manifest_sha256: str
    multi_year_manifest_sha256: str


class HumanDecisionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    acceptance_fact_id: UUID
    review_package: ValidationReviewPackage
    acceptance_decision: AcceptanceDecision
    decision_manifest_payload: dict[str, Any]
    decision_manifest_sha256: str


class HumanValidationGateService:
    """Builds review evidence and records governance-owned immutable decisions."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def evaluate_review_package(self, validation_run_id: UUID) -> ValidationReviewPackage:
        run = self.session.get(HistoricalValidationRun, validation_run_id)
        ledger = self.session.scalar(select(HistoricalValidationConfidenceLedger).where(
            HistoricalValidationConfidenceLedger.validation_run_id == validation_run_id
        ))
        if run is None or ledger is None:
            raise ValueError("validation run and confidence ledger must resolve canonically")
        if ledger.validation_run_id != run.validation_run_id:
            raise ValueError("confidence ledger does not belong to validation run")
        return ValidationReviewPackage(
            validation_run_id=run.validation_run_id,
            confidence_ledger_id=ledger.confidence_ledger_id,
            confidence_policy_version=ledger.confidence_policy_version,
            confidence_score=Decimal(ledger.composite_confidence_score),
            confidence_tier=ledger.confidence_tier,
            hard_gate_passed=ledger.hard_gate_passed,
            gate_eligible=ledger.gate_eligible,
            hard_gate_evaluations=ledger.hard_gate_evaluations_json,
            confidence_manifest_sha256=ledger.confidence_manifest_sha256,
            scorecard_manifest_sha256=run.scorecard_manifest_sha256,
            multi_year_manifest_sha256=run.multi_year_manifest_sha256,
        )

    def record_human_decision(
        self,
        *,
        validation_run_id: UUID,
        human_reviewer_identity: str,
        acceptance_decision: AcceptanceDecision,
        decision_rationale: str,
        decided_at: datetime,
    ) -> HumanDecisionResult:
        reviewer = human_reviewer_identity.strip()
        rationale = decision_rationale.strip()
        if not reviewer or len(reviewer) > 128:
            raise ValueError("human reviewer identity must be non-empty and at most 128 characters")
        if not rationale:
            raise ValueError("decision rationale must be non-empty")
        if decided_at.tzinfo is None:
            raise ValueError("decided_at must be timezone-aware")
        package = self.evaluate_review_package(validation_run_id)
        if acceptance_decision == "ACCEPTED_FOR_LIVE" and not (
            package.gate_eligible and package.hard_gate_passed and package.confidence_score >= Decimal("80.00")
        ):
            raise ValueError("ACCEPTED_FOR_LIVE requires confidence >= 80, all hard gates, and gate eligibility")
        acceptance_id = uuid5(NAMESPACE_URL, f"kairo:human-validation:{validation_run_id}")
        payload = {
            "manifest_version": "HUMAN-VALIDATION-DECISION-v1",
            "acceptance_fact_id": acceptance_id,
            "validation_run_id": package.validation_run_id,
            "confidence_ledger_id": package.confidence_ledger_id,
            "human_reviewer_identity": reviewer,
            "acceptance_decision": acceptance_decision,
            "decision_rationale": rationale,
            "confidence_score_at_review": package.confidence_score,
            "hard_gates_passed_at_review": package.hard_gate_passed,
            "gate_eligibility_at_review": package.gate_eligible,
            "bound_confidence_manifest_sha256": package.confidence_manifest_sha256,
            "bound_scorecard_manifest_sha256": package.scorecard_manifest_sha256,
            "bound_multi_year_manifest_sha256": package.multi_year_manifest_sha256,
            "decided_at": decided_at.astimezone(timezone.utc),
        }
        digest = decision_manifest_sha256(payload)
        existing = self.session.get(HistoricalValidationAcceptanceFact, acceptance_id)
        if existing is not None:
            if existing.decision_manifest_sha256 != digest:
                raise ValueError("conflicting immutable human decision for validation run")
            return HumanDecisionResult(acceptance_fact_id=acceptance_id, review_package=package, acceptance_decision=acceptance_decision, decision_manifest_payload=_canonical(payload), decision_manifest_sha256=digest)
        self.session.add(HistoricalValidationAcceptanceFact(
            acceptance_fact_id=acceptance_id,
            confidence_ledger_id=package.confidence_ledger_id,
            validation_run_id=package.validation_run_id,
            human_reviewer_identity=reviewer,
            acceptance_decision=acceptance_decision,
            decision_rationale=rationale,
            confidence_score_at_review=package.confidence_score,
            hard_gates_passed_at_review=package.hard_gate_passed,
            gate_eligibility_at_review=package.gate_eligible,
            bound_confidence_manifest_sha256=package.confidence_manifest_sha256,
            bound_scorecard_manifest_sha256=package.scorecard_manifest_sha256,
            bound_multi_year_manifest_sha256=package.multi_year_manifest_sha256,
            decision_manifest_sha256=digest,
            decided_at=decided_at,
        ))
        self.session.flush()
        return HumanDecisionResult(acceptance_fact_id=acceptance_id, review_package=package, acceptance_decision=acceptance_decision, decision_manifest_payload=_canonical(payload), decision_manifest_sha256=digest)
