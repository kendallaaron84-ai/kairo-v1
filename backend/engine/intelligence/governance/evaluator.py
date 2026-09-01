import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.intelligence import (
    CellIntelligenceAuthorityEvent,
    IntelligenceAuthorityDecision,
    IntelligenceAuthorityProposal,
    IntelligenceResearchRun,
    IntelligenceStatefulReplayRun,
    OrderContextEvaluation,
)
from app.db.models.ledger import OrderIntent
from app.db.models.projections import CapitalCell
from engine.intelligence.governance.policy import AuthorityPolicyV1


def serialize_governance_manifest(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def governance_manifest_sha256(payload: dict[str, object]) -> str:
    return hashlib.sha256(serialize_governance_manifest(payload)).hexdigest()


class PromotionCriteriaEvaluator:
    """Creates immutable proposals; it never grants runtime authority."""

    TARGET_AUTHORITY = "VETO_ONLY"

    def __init__(self, session: Session, *, clock: Callable[[], datetime] | None = None) -> None:
        self.db = session
        self.clock = clock or (lambda: datetime.now(UTC))
        self.last_manifest: dict[str, object] | None = None

    def evaluate(
        self,
        *,
        cell_id: UUID,
        step5_run_id: UUID,
        step5_5_run_id: UUID,
    ) -> IntelligenceAuthorityProposal:
        cell = self.db.get(CapitalCell, cell_id)
        step5 = self.db.get(IntelligenceResearchRun, step5_run_id)
        step55 = self.db.get(IntelligenceStatefulReplayRun, step5_5_run_id)
        if cell is None or step5 is None or step55 is None:
            raise ValueError("cell and both canonical research runs must resolve")
        if step5.cell_id is None or step5.cell_id != cell_id or step55.cell_id != cell_id:
            raise ValueError("Step 5 and Step 5.5 evidence must reference the same cell")
        if (
            step5.sample_start_time != step55.sample_start_time
            or step5.sample_end_time != step55.sample_end_time
        ):
            raise ValueError("Step 5 and Step 5.5 evidence windows must match exactly")

        eligible = (
            select(OrderContextEvaluation.evaluated_at, OrderIntent.strategy_id, OrderIntent.strategy_version)
            .join(OrderIntent, OrderIntent.intent_id == OrderContextEvaluation.intent_id)
            .where(
                OrderIntent.cell_id == cell_id,
                OrderContextEvaluation.counterfactual_opinion == "WOULD_HAVE_VETOED",
                OrderContextEvaluation.veto_reason_code.in_(AuthorityPolicyV1.ALLOWED_REASON_CODES),
                OrderContextEvaluation.evaluated_at >= step5.sample_start_time,
                OrderContextEvaluation.evaluated_at <= step5.sample_end_time,
            )
        )
        rows = list(self.db.execute(eligible))
        strategy_identities = {(row.strategy_id, row.strategy_version) for row in rows}
        expected_strategy = (cell.strategy_id, cell.strategy_version)
        if strategy_identities and strategy_identities != {expected_strategy}:
            raise ValueError("research evidence strategy identity does not match the canonical cell")
        months = len({(row.evaluated_at.year, row.evaluated_at.month) for row in rows})
        allowed_veto_count = len(rows)
        criteria = (
            step5.total_veto_opportunities >= 30
            and allowed_veto_count == step5.total_veto_opportunities
            and months >= 3
            and step5.net_alpha_usd > Decimal("0.00")
            and step55.stateful_net_alpha_usd > Decimal("0.00")
            and step55.drawdown_reduction_usd >= Decimal("0.00")
            and step5.veto_precision_pct >= Decimal("60.00")
        )
        proposed_at = self.clock()
        self._aware(proposed_at, "proposal clock")
        manifest: dict[str, object] = {
            "manifest_type": "VETO_ONLY_AUTHORITY_PROPOSAL",
            "policy_version": AuthorityPolicyV1.POLICY_VERSION,
            "target_authority": self.TARGET_AUTHORITY,
            "cell_id": str(cell_id),
            "strategy_identity": {
                "strategy_id": cell.strategy_id,
                "strategy_version": cell.strategy_version,
            },
            "evidence": {
                "step5_run_id": str(step5_run_id),
                "step5_manifest_sha256": step5.research_manifest_sha256,
                "step5_5_run_id": str(step5_5_run_id),
                "step5_5_manifest_sha256": step55.stateful_replay_manifest_sha256,
                "sample_start_time": step5.sample_start_time.isoformat(),
                "sample_end_time": step5.sample_end_time.isoformat(),
            },
            "criteria": {
                "evaluated_veto_opportunities": step5.total_veto_opportunities,
                "allowed_policy_veto_opportunities": allowed_veto_count,
                "distinct_trading_months": months,
                "trade_removal_alpha_usd": str(step5.net_alpha_usd),
                "stateful_alpha_usd": str(step55.stateful_net_alpha_usd),
                "drawdown_reduction_usd": str(step55.drawdown_reduction_usd),
                "veto_precision_pct": str(step5.veto_precision_pct),
                "criteria_passed": criteria,
            },
            "proposed_at": proposed_at.isoformat(),
        }
        manifest_hash = governance_manifest_sha256(manifest)
        proposal_id = uuid5(NAMESPACE_URL, f"kairo:intel-authority-proposal:{manifest_hash}")
        self.last_manifest = {"proposal_id": str(proposal_id), **manifest}
        existing = self.db.get(IntelligenceAuthorityProposal, proposal_id)
        if existing is not None:
            if existing.proposal_manifest_sha256 != manifest_hash:
                raise ValueError("conflicting deterministic authority proposal")
            return existing
        proposal = IntelligenceAuthorityProposal(
            proposal_id=proposal_id,
            cell_id=cell_id,
            target_authority=self.TARGET_AUTHORITY,
            policy_version=AuthorityPolicyV1.POLICY_VERSION,
            step5_run_id=step5_run_id,
            step5_5_run_id=step5_5_run_id,
            evaluated_veto_opportunities=step5.total_veto_opportunities,
            distinct_trading_months=months,
            sample_start_time=step5.sample_start_time,
            sample_end_time=step5.sample_end_time,
            trade_removal_alpha_usd=step5.net_alpha_usd,
            stateful_alpha_usd=step55.stateful_net_alpha_usd,
            drawdown_reduction_usd=step55.drawdown_reduction_usd,
            veto_precision_pct=step5.veto_precision_pct,
            criteria_passed=criteria,
            proposal_manifest_sha256=manifest_hash,
            proposed_at=proposed_at,
        )
        self.db.add(proposal)
        self.db.flush()
        return proposal

    @staticmethod
    def _aware(value: datetime, field: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")


class AuthorityGovernanceService:
    """Persists explicit human decisions and append-only lifecycle events."""

    def __init__(self, session: Session, *, clock: Callable[[], datetime] | None = None) -> None:
        self.db = session
        self.clock = clock or (lambda: datetime.now(UTC))
        self.last_manifest: dict[str, object] | None = None

    def decide(self, proposal_id: UUID, decision: str, operator_identity: str) -> IntelligenceAuthorityDecision:
        proposal = self.db.get(IntelligenceAuthorityProposal, proposal_id)
        if proposal is None:
            raise ValueError("authority proposal does not resolve")
        normalized = decision.strip().upper()
        operator = operator_identity.strip()
        if normalized not in {"APPROVED", "REJECTED"} or not operator:
            raise ValueError("decision and human operator identity are required")
        decided_at = self.clock()
        payload: dict[str, object] = {
            "manifest_type": "VETO_ONLY_HUMAN_DECISION",
            "proposal_id": str(proposal_id),
            "approved_proposal_manifest_sha256": proposal.proposal_manifest_sha256,
            "decision": normalized,
            "operator_identity": operator,
            "decided_at": decided_at.isoformat(),
        }
        digest = governance_manifest_sha256(payload)
        decision_id = uuid5(NAMESPACE_URL, f"kairo:intel-authority-decision:{digest}")
        self.last_manifest = {"decision_id": str(decision_id), **payload}
        row = IntelligenceAuthorityDecision(
            decision_id=decision_id,
            proposal_id=proposal_id,
            decision=normalized,
            operator_identity=operator,
            approved_proposal_manifest_sha256=proposal.proposal_manifest_sha256,
            decision_manifest_sha256=digest,
            decided_at=decided_at,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def record_event(
        self,
        *,
        cell_id: UUID,
        event_type: str,
        operator_identity: str,
        decision_id: UUID | None = None,
        effective_at: datetime | None = None,
    ) -> CellIntelligenceAuthorityEvent:
        event = event_type.strip().upper()
        operator = operator_identity.strip()
        at = effective_at or self.clock()
        if event not in {"GRANTED", "REVOKED", "EXPIRED"} or not operator:
            raise ValueError("valid lifecycle event and operator identity are required")
        mode = "VETO_ONLY" if event == "GRANTED" else "OBSERVE_ONLY"
        payload: dict[str, object] = {
            "manifest_type": "CELL_INTELLIGENCE_AUTHORITY_EVENT",
            "cell_id": str(cell_id),
            "decision_id": str(decision_id) if decision_id else None,
            "event_type": event,
            "authority_mode": mode,
            "policy_version": AuthorityPolicyV1.POLICY_VERSION,
            "operator_identity": operator,
            "effective_at": at.isoformat(),
        }
        digest = governance_manifest_sha256(payload)
        event_id = uuid5(NAMESPACE_URL, f"kairo:intel-authority-event:{digest}")
        self.last_manifest = {"event_id": str(event_id), **payload}
        row = CellIntelligenceAuthorityEvent(
            event_id=event_id,
            cell_id=cell_id,
            decision_id=decision_id,
            event_type=event,
            authority_mode=mode,
            policy_version=AuthorityPolicyV1.POLICY_VERSION,
            operator_identity=operator,
            effective_at=at,
            event_manifest_sha256=digest,
            created_at=self.clock(),
        )
        self.db.add(row)
        self.db.flush()
        return row
