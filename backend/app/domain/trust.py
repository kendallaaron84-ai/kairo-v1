from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import TrustOutcome


class TrustEvaluationFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    evaluation_id: UUID = Field(default_factory=uuid4)
    cell_id: UUID
    policy_id: UUID
    policy_version: str
    score: Decimal | None
    outcome: TrustOutcome
    eligible_for_promotion: bool
    evidence_trade_count: int = Field(ge=0)
    disqualifiers: list[str] = Field(default_factory=list)
    factor_breakdown: dict = Field(default_factory=dict)
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    details: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def zero_evidence_has_no_score(self) -> "TrustEvaluationFact":
        if self.evidence_trade_count == 0:
            if self.score is not None or self.eligible_for_promotion:
                raise ValueError("zero-evidence evaluations cannot score or promote")
        elif self.score is None:
            raise ValueError("evaluations with evidence require a score")
        return self


class TrustPolicyConfiguration(BaseModel):
    policy_id: UUID = Field(default_factory=uuid4)
    version_tag: str
    name: str
    policy_document: dict = Field(default_factory=dict)
    effective_from: datetime = Field(default_factory=lambda: datetime.now(UTC))
    retired_at: datetime | None = None
