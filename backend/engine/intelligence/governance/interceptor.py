from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.intelligence import (
    CellIntelligenceAuthorityEvent,
    IntelligenceEvidenceLedger,
    MarketContextAssessment,
    OrderContextEvaluation,
)
from app.db.models.ledger import CellEvent, OrderIntent
from engine.intelligence.governance.policy import AuthorityPolicyV1


@dataclass(frozen=True)
class InterceptionResult:
    route_to_broker: bool
    authority_mode: str
    reason_code: str | None = None
    veto_fact_id: UUID | None = None


class RuntimeAuthorityInterceptor:
    """The sole runtime suppression boundary; it cannot originate or modify intents."""

    def __init__(self, session: Session, *, clock=None) -> None:
        self.db = session
        self.clock = clock or (lambda: datetime.now(UTC))

    def intercept(self, intent_id: UUID, *, evaluated_at: datetime | None = None) -> InterceptionResult:
        at = evaluated_at or self.clock()
        intent = self.db.get(OrderIntent, intent_id)
        if intent is None:
            raise ValueError("order intent does not resolve")
        authority = self.db.scalar(
            select(CellIntelligenceAuthorityEvent)
            .where(
                CellIntelligenceAuthorityEvent.cell_id == intent.cell_id,
                CellIntelligenceAuthorityEvent.effective_at <= at,
            )
            .order_by(
                CellIntelligenceAuthorityEvent.effective_at.desc(),
                CellIntelligenceAuthorityEvent.created_at.desc(),
                CellIntelligenceAuthorityEvent.event_id.desc(),
            )
            .limit(1)
        )
        if authority is None or authority.authority_mode != "VETO_ONLY":
            return InterceptionResult(True, "OBSERVE_ONLY")
        if authority.policy_version != AuthorityPolicyV1.POLICY_VERSION:
            return InterceptionResult(True, "OBSERVE_ONLY")

        evaluation = self.db.scalar(
            select(OrderContextEvaluation)
            .where(OrderContextEvaluation.intent_id == intent_id)
            .limit(1)
        )
        if (
            evaluation is None
            or evaluation.counterfactual_opinion != "WOULD_HAVE_VETOED"
            or evaluation.veto_reason_code not in AuthorityPolicyV1.ALLOWED_REASON_CODES
        ):
            return InterceptionResult(True, "VETO_ONLY")
        assessment = self.db.get(MarketContextAssessment, evaluation.assessment_id)
        if (
            assessment is None
            or assessment.cell_id != intent.cell_id
            or assessment.authority_mode != "OBSERVE_ONLY"
            or not assessment.macro_window_active
            or assessment.primary_event_id is None
        ):
            return InterceptionResult(True, "VETO_ONLY")
        evidence = self.db.get(IntelligenceEvidenceLedger, assessment.primary_event_id)
        event_type = AuthorityPolicyV1.classify_event(evidence) if evidence else None
        if event_type not in AuthorityPolicyV1.ALLOWED_EVENT_TYPES:
            return InterceptionResult(True, "VETO_ONLY")
        if not (
            evidence.effective_at - timedelta(minutes=AuthorityPolicyV1.WINDOW_BEFORE_MINUTES)
            <= at
            <= evidence.effective_at + timedelta(minutes=AuthorityPolicyV1.WINDOW_AFTER_MINUTES)
        ):
            return InterceptionResult(True, "VETO_ONLY")

        fact_id = uuid5(
            NAMESPACE_URL,
            f"kairo:intent-vetoed:{intent_id}:{evaluation.evaluation_id}:{authority.event_id}",
        )
        if self.db.get(CellEvent, fact_id) is None:
            self.db.add(CellEvent(
                event_id=fact_id,
                cell_id=intent.cell_id,
                event_type="INTENT_VETOED",
                occurred_at=at,
                payload={
                    "intent_id": str(intent_id),
                    "evaluation_id": str(evaluation.evaluation_id),
                    "assessment_id": str(assessment.assessment_id),
                    "authority_event_id": str(authority.event_id),
                    "policy_version": AuthorityPolicyV1.POLICY_VERSION,
                    "reason_code": evaluation.veto_reason_code,
                    "event_type": event_type,
                    "runtime_effect": "SUPPRESS_ONLY",
                },
            ))
            self.db.flush()
        return InterceptionResult(False, "VETO_ONLY", evaluation.veto_reason_code, fact_id)
