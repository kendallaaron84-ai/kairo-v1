import hashlib
import json
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class InvestigationVerdict(StrEnum):
    CONFIRMED = "CONFIRMED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class FindingType(StrEnum):
    SUPPORTING = "SUPPORTING"
    CONTRADICTORY = "CONTRADICTORY"
    GAP_IDENTIFIED = "GAP_IDENTIFIED"
    SUPERSEDED_FACT = "SUPERSEDED_FACT"


class CitationRole(StrEnum):
    PRIMARY_PROOF = "PRIMARY_PROOF"
    CONTRADICTION = "CONTRADICTION"
    CONTEXT = "CONTEXT"


class TemporalStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    HISTORICAL_CONTEXT = "HISTORICAL_CONTEXT"


class CitationPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: UUID
    citation_role: CitationRole
    temporal_status: TemporalStatus
    citation_relevance: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2)


class FindingPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    finding_type: FindingType
    claim_assertion: str = Field(min_length=1)
    finding_narrative: str = Field(min_length=1)
    search_scope_json: dict[str, Any] | None = None
    citations: tuple[CitationPayload, ...] = ()

    @field_validator("claim_assertion", "finding_narrative")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("finding text cannot be blank")
        return value.strip()

    @model_validator(mode="after")
    def validate_evidence_topology(self) -> "FindingPayload":
        if self.finding_type is FindingType.GAP_IDENTIFIED:
            if not self.search_scope_json:
                raise ValueError("GAP_IDENTIFIED requires a non-empty search_scope_json")
            if self.citations:
                raise ValueError("GAP_IDENTIFIED cannot contain fabricated citations")
        elif self.finding_type in (
            FindingType.SUPPORTING,
            FindingType.CONTRADICTORY,
        ) and len(self.citations) < 1:
            raise ValueError(f"{self.finding_type.value} requires at least 1 citation")
        elif (
            self.finding_type is FindingType.SUPERSEDED_FACT
            and len(self.citations) < 2
        ):
            raise ValueError("SUPERSEDED_FACT requires at least 2 citations")
        if len({citation.event_id for citation in self.citations}) != len(self.citations):
            raise ValueError("a finding cannot cite the same evidence event twice")
        return self


def derive_verdict(
    findings: list[object], citations: list[object]
) -> InvestigationVerdict:
    if not findings or all(
        _value(finding.finding_type) == FindingType.GAP_IDENTIFIED.value
        for finding in findings
    ):
        return InvestigationVerdict.INSUFFICIENT_EVIDENCE

    finding_types = {_value(finding.finding_type) for finding in findings}
    has_supporting = FindingType.SUPPORTING.value in finding_types
    has_contradictory = FindingType.CONTRADICTORY.value in finding_types
    has_gaps = FindingType.GAP_IDENTIFIED.value in finding_types
    active_supporting_proof = any(
        _value(citation.citation_role) == CitationRole.PRIMARY_PROOF.value
        and _value(citation.temporal_status) == TemporalStatus.ACTIVE.value
        for citation in citations
    )
    active_contradiction_proof = any(
        _value(citation.citation_role) == CitationRole.CONTRADICTION.value
        and _value(citation.temporal_status) == TemporalStatus.ACTIVE.value
        for citation in citations
    )

    if active_contradiction_proof and not has_supporting:
        return InvestigationVerdict.UNSUPPORTED
    if active_supporting_proof and not has_contradictory and not has_gaps:
        return InvestigationVerdict.CONFIRMED
    if has_supporting and (
        has_contradictory or has_gaps or not active_supporting_proof
    ):
        return InvestigationVerdict.PARTIALLY_SUPPORTED
    return InvestigationVerdict.INSUFFICIENT_EVIDENCE


def compute_case_manifest_sha256(
    case: object,
    findings: list[object],
    citations: list[object],
    conclusion: object,
    evidence_hashes: dict[UUID, str],
) -> str:
    payload = {
        "case_id": str(case.case_id),
        "case_number": case.case_number,
        "query_prompt": case.query_prompt,
        "hypothesis_claim": case.hypothesis_claim,
        "target_symbol": case.target_symbol,
        "target_theme": case.target_theme,
        "opened_at": case.opened_at.isoformat(),
        "findings": [
            {
                "sequence_num": finding.sequence_num,
                "finding_type": _value(finding.finding_type),
                "claim_assertion": finding.claim_assertion,
                "finding_narrative": finding.finding_narrative,
                "search_scope": finding.search_scope_json,
                "citations": sorted(
                    [
                        {
                            "event_id": str(citation.event_id),
                            "evidence_content_sha256": evidence_hashes[citation.event_id],
                            "citation_role": _value(citation.citation_role),
                            "temporal_status": _value(citation.temporal_status),
                            "citation_relevance": str(citation.citation_relevance),
                        }
                        for citation in citations
                        if citation.finding_id == finding.finding_id
                    ],
                    key=lambda item: item["event_id"],
                ),
            }
            for finding in sorted(findings, key=lambda item: item.sequence_num)
        ],
        "conclusion": {
            "verdict": _value(conclusion.verdict),
            "confidence_score": str(conclusion.confidence_score),
            "materiality_scope": _value(conclusion.materiality_scope),
            "time_horizon": _value(conclusion.time_horizon),
            "synthesis_summary": conclusion.synthesis_summary,
            "closed_at": conclusion.closed_at.isoformat(),
        },
    }
    serialized = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _value(value: object) -> str:
    return str(value.value) if isinstance(value, StrEnum) else str(value)
