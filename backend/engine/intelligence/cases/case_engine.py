from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.intelligence import (
    IntelligenceCaseConclusion,
    IntelligenceCaseFinding,
    IntelligenceEvidenceLedger,
    IntelligenceFindingCitation,
    IntelligenceInvestigationCase,
)
from engine.intelligence.cases.models import (
    FindingPayload,
    FindingType,
    compute_case_manifest_sha256,
    derive_verdict,
)
from engine.intelligence.models import ImpactScope, TimeHorizon


CENT = Decimal("0.01")


class CaseEngine:
    """Append-only evidence synthesis with strictly OBSERVE_ONLY authority."""

    authority_mode = "OBSERVE_ONLY"

    def __init__(
        self,
        db_session: Session,
        *,
        clock: Callable[[], datetime] | None = None,
        identity_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self.db = db_session
        self.clock = clock or (lambda: datetime.now(UTC))
        self.identity_factory = identity_factory or uuid4

    def open_case(
        self,
        query_prompt: str,
        target_symbol: str | None,
        target_theme: str | None,
        hypothesis_claim: str,
    ) -> IntelligenceInvestigationCase:
        opened_at = self._now()
        case_id = self.identity_factory()
        row = IntelligenceInvestigationCase(
            case_id=case_id,
            case_number=f"CASE-{opened_at:%Y%m%d}-{case_id.hex[:12].upper()}",
            query_prompt=self._required(query_prompt, "query_prompt"),
            hypothesis_claim=self._required(hypothesis_claim, "hypothesis_claim"),
            target_symbol=self._optional_upper(target_symbol),
            target_theme=self._optional_upper(target_theme),
            opened_at=opened_at,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def add_findings(
        self, case_id: UUID, findings: Sequence[FindingPayload]
    ) -> list[IntelligenceCaseFinding]:
        self._open_case(case_id)
        if not findings:
            return []
        next_sequence = (
            self.db.scalar(select(func.max(IntelligenceCaseFinding.sequence_num)).where(
                IntelligenceCaseFinding.case_id == case_id
            ))
            or 0
        ) + 1
        created: list[IntelligenceCaseFinding] = []
        for offset, payload in enumerate(findings):
            self._require_canonical_evidence(payload)
            created_at = self._now()
            finding = IntelligenceCaseFinding(
                finding_id=self.identity_factory(),
                case_id=case_id,
                finding_type=payload.finding_type.value,
                claim_assertion=payload.claim_assertion,
                finding_narrative=payload.finding_narrative,
                search_scope_json=payload.search_scope_json,
                sequence_num=next_sequence + offset,
                created_at=created_at,
            )
            self.db.add(finding)
            self.db.flush()
            for citation in payload.citations:
                self.db.add(IntelligenceFindingCitation(
                    citation_id=self.identity_factory(),
                    finding_id=finding.finding_id,
                    event_id=citation.event_id,
                    citation_role=citation.citation_role.value,
                    temporal_status=citation.temporal_status.value,
                    citation_relevance=citation.citation_relevance.quantize(CENT),
                    created_at=created_at,
                ))
            self.db.flush()
            created.append(finding)
        return created

    def conclude_case(
        self,
        case_id: UUID,
        materiality_scope: ImpactScope,
        time_horizon: TimeHorizon,
        synthesis_summary: str,
        confidence_score: Decimal,
    ) -> IntelligenceCaseConclusion:
        case = self._open_case(case_id)
        confidence = Decimal(confidence_score).quantize(CENT)
        if not Decimal("0") <= confidence <= Decimal("100"):
            raise ValueError("confidence_score must be between 0 and 100")
        findings = list(self.db.scalars(
            select(IntelligenceCaseFinding)
            .where(IntelligenceCaseFinding.case_id == case_id)
            .order_by(IntelligenceCaseFinding.sequence_num)
        ))
        citations = list(self.db.scalars(
            select(IntelligenceFindingCitation)
            .join(
                IntelligenceCaseFinding,
                IntelligenceCaseFinding.finding_id
                == IntelligenceFindingCitation.finding_id,
            )
            .where(IntelligenceCaseFinding.case_id == case_id)
            .order_by(IntelligenceFindingCitation.event_id)
        ))
        event_ids = {citation.event_id for citation in citations}
        evidence_hashes = dict(self.db.execute(
            select(
                IntelligenceEvidenceLedger.event_id,
                IntelligenceEvidenceLedger.raw_content_sha256,
            ).where(IntelligenceEvidenceLedger.event_id.in_(event_ids))
        )) if event_ids else {}
        if set(evidence_hashes) != event_ids:
            raise ValueError("case contains a citation without canonical evidence")

        conclusion = IntelligenceCaseConclusion(
            conclusion_id=self.identity_factory(),
            case_id=case_id,
            verdict=derive_verdict(findings, citations).value,
            confidence_score=confidence,
            materiality_scope=materiality_scope.value,
            time_horizon=time_horizon.value,
            synthesis_summary=self._required(
                synthesis_summary, "synthesis_summary"
            ),
            case_manifest_sha256="",
            closed_at=self._now(),
        )
        conclusion.case_manifest_sha256 = compute_case_manifest_sha256(
            case, findings, citations, conclusion, evidence_hashes
        )
        self.db.add(conclusion)
        self.db.flush()
        return conclusion

    def _open_case(self, case_id: UUID) -> IntelligenceInvestigationCase:
        case = self.db.get(IntelligenceInvestigationCase, case_id)
        if case is None:
            raise ValueError(f"unknown investigation case {case_id}")
        if self.db.scalar(select(IntelligenceCaseConclusion.conclusion_id).where(
            IntelligenceCaseConclusion.case_id == case_id
        )) is not None:
            raise ValueError("investigation case is already concluded")
        return case

    def _require_canonical_evidence(self, finding: FindingPayload) -> None:
        if finding.finding_type is FindingType.GAP_IDENTIFIED:
            return
        event_ids = {citation.event_id for citation in finding.citations}
        existing = set(self.db.scalars(
            select(IntelligenceEvidenceLedger.event_id).where(
                IntelligenceEvidenceLedger.event_id.in_(event_ids)
            )
        ))
        if existing != event_ids:
            raise ValueError("finding citation references nonexistent evidence")

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("case clock must return a timezone-aware datetime")
        return value

    @staticmethod
    def _required(value: str, field: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field} cannot be blank")
        return normalized

    @staticmethod
    def _optional_upper(value: str | None) -> str | None:
        normalized = value.strip().upper() if value else ""
        return normalized or None
