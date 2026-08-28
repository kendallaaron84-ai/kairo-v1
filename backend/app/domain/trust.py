from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import TrustOutcome


class TrustEvaluationFact(BaseModel):
    model_config = ConfigDict(frozen=True)

    evaluation_id: UUID = Field(default_factory=uuid4)
    cell_id: UUID
    policy_id: UUID
    policy_version: str
    score: Decimal
    outcome: TrustOutcome
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    details: dict = Field(default_factory=dict)


class TrustPolicyConfiguration(BaseModel):
    policy_id: UUID = Field(default_factory=uuid4)
    version_tag: str
    name: str
    policy_document: dict = Field(default_factory=dict)
    effective_from: datetime = Field(default_factory=lambda: datetime.now(UTC))
    retired_at: datetime | None = None
