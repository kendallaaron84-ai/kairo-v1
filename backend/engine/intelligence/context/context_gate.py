import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.intelligence import (
    IntelligenceCaseConclusion,
    IntelligenceEvidenceLedger,
    IntelligenceInvestigationCase,
    MarketContextAssessment,
    OrderContextEvaluation,
)
from app.db.models.ledger import OrderIntent


class ContextGate:
    """Deterministic context observation with no execution or Governor authority."""

    authority_mode = "OBSERVE_ONLY"

    def __init__(
        self,
        db_session: Session,
        macro_buffer_minutes: int = 5,
        *,
        clock: Callable[[], datetime] | None = None,
        identity_factory: Callable[[], UUID] | None = None,
    ) -> None:
        if macro_buffer_minutes < 0:
            raise ValueError("macro_buffer_minutes cannot be negative")
        self.db = db_session
        self.macro_buffer_minutes = macro_buffer_minutes
        self.clock = clock or (lambda: datetime.now(UTC))
        self.identity_factory = identity_factory or uuid4

    def evaluate_market_context(
        self,
        cell_id: UUID,
        current_time: datetime,
        symbol: str | None = None,
    ) -> MarketContextAssessment:
        self._require_aware(current_time, "current_time")
        window = timedelta(minutes=self.macro_buffer_minutes)
        critical_macro = self.db.scalar(
            select(IntelligenceEvidenceLedger)
            .where(
                IntelligenceEvidenceLedger.event_type == "MACRO",
                IntelligenceEvidenceLedger.urgency == "CRITICAL",
                IntelligenceEvidenceLedger.observed_at <= current_time,
                IntelligenceEvidenceLedger.effective_at >= current_time - window,
                IntelligenceEvidenceLedger.effective_at <= current_time + window,
            )
            .order_by(
                IntelligenceEvidenceLedger.effective_at,
                IntelligenceEvidenceLedger.event_id,
            )
            .limit(1)
        )

        active_conclusion = None
        if symbol and symbol.strip():
            active_conclusion = self.db.scalar(
                select(IntelligenceCaseConclusion)
                .join(
                    IntelligenceInvestigationCase,
                    IntelligenceCaseConclusion.case_id
                    == IntelligenceInvestigationCase.case_id,
                )
                .where(
                    IntelligenceInvestigationCase.target_symbol
                    == symbol.strip().upper(),
                    IntelligenceCaseConclusion.closed_at <= current_time,
                )
                .order_by(
                    IntelligenceCaseConclusion.closed_at.desc(),
                    IntelligenceCaseConclusion.conclusion_id,
                )
                .limit(1)
            )

        risk_posture = "NORMAL"
        macro_active = False
        primary_event_id = None
        active_case_id = None
        summary = "Operating under normal macro and asset baseline."
        if critical_macro is not None:
            risk_posture = "HIGH_EVENT_RISK"
            macro_active = True
            primary_event_id = critical_macro.event_id
            summary = (
                f"Critical macro window active: {critical_macro.title} "
                f"(effective {critical_macro.effective_at.isoformat()})"
            )
        elif active_conclusion is not None and active_conclusion.verdict in {
            "UNSUPPORTED",
            "PARTIALLY_SUPPORTED",
            "INSUFFICIENT_EVIDENCE",
        }:
            risk_posture = "ELEVATED"
            active_case_id = active_conclusion.case_id
            summary = (
                f"Elevated company risk: investigation {active_conclusion.case_id} "
                f"concluded with verdict {active_conclusion.verdict}"
            )

        manifest_hash = compute_assessment_manifest_sha256(
            cell_id=cell_id,
            evaluated_at=current_time,
            risk_posture=risk_posture,
            macro_window_active=macro_active,
            primary_event_id=primary_event_id,
            active_case_id=active_case_id,
            summary=summary,
        )
        created_at = self.clock()
        self._require_aware(created_at, "context clock")
        assessment = MarketContextAssessment(
            assessment_id=self.identity_factory(),
            cell_id=cell_id,
            risk_posture=risk_posture,
            authority_mode=self.authority_mode,
            macro_window_active=macro_active,
            primary_event_id=primary_event_id,
            active_case_id=active_case_id,
            assessment_summary=summary,
            assessment_manifest_sha256=manifest_hash,
            evaluated_at=current_time,
            created_at=created_at,
        )
        self.db.add(assessment)
        self.db.flush()
        return assessment

    def evaluate_order_intent(
        self,
        intent_id: UUID,
        assessment: MarketContextAssessment,
    ) -> OrderContextEvaluation:
        intent = self.db.get(OrderIntent, intent_id)
        if intent is None:
            raise ValueError(f"order intent {intent_id} does not exist")
        canonical_assessment = self.db.get(
            MarketContextAssessment, assessment.assessment_id
        )
        if canonical_assessment is None:
            raise ValueError("market context assessment is not persisted")

        if canonical_assessment.macro_window_active:
            opinion = "WOULD_HAVE_VETOED"
            reason = "CRITICAL_MACRO_WINDOW_ACTIVE"
        elif canonical_assessment.risk_posture == "ELEVATED":
            opinion = "NO_OPINION"
            reason = None
        else:
            opinion = "WOULD_HAVE_AUTHORIZED"
            reason = None
        evaluation = OrderContextEvaluation(
            evaluation_id=self.identity_factory(),
            intent_id=intent.intent_id,
            assessment_id=canonical_assessment.assessment_id,
            counterfactual_opinion=opinion,
            veto_reason_code=reason,
            evaluated_at=canonical_assessment.evaluated_at,
        )
        self.db.add(evaluation)
        self.db.flush()
        return evaluation

    @staticmethod
    def _require_aware(value: datetime, field: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")


def compute_assessment_manifest_sha256(
    *,
    cell_id: UUID,
    evaluated_at: datetime,
    risk_posture: str,
    macro_window_active: bool,
    primary_event_id: UUID | None,
    active_case_id: UUID | None,
    summary: str,
) -> str:
    payload = {
        "active_case_id": str(active_case_id) if active_case_id else None,
        "cell_id": str(cell_id),
        "evaluated_at": evaluated_at.isoformat(),
        "macro_window_active": macro_window_active,
        "primary_event_id": str(primary_event_id) if primary_event_id else None,
        "risk_posture": risk_posture,
        "summary": summary,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()
