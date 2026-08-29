from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import OrderSide


class EvidenceStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class SafetyEligibility(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    DISQUALIFIED = "DISQUALIFIED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class AutonomyTier(StrEnum):
    APPRENTICE = "APPRENTICE"
    GUARDED = "GUARDED"
    AUTONOMOUS = "AUTONOMOUS"


class ClosedTradeEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    trade_id: UUID
    closed_at: datetime
    realized_pnl_usd: Decimal | None = None
    planned_risk_usd: Decimal | None = Field(default=None, gt=0)
    mfe_r: Decimal | None = Field(default=None, ge=0)
    mae_r: Decimal | None = Field(default=None, ge=0)
    regime: str | None = None
    strategy_compliant: bool | None = None
    settlement_verified: bool | None = None


class EquityPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    equity: Decimal = Field(gt=0)


class ExecutionEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    fill_id: UUID
    filled_at: datetime | None = None
    side: OrderSide
    fill_price: Decimal = Field(gt=0)
    reference_price: Decimal | None = Field(default=None, gt=0)
    quantity: Decimal = Field(gt=0)
    contract_multiplier: Decimal = Field(default=Decimal("1"), gt=0)


class SafetyAuditEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    broker_reconciliation_verified: bool | None = None
    post_halt_trading_verified_clean: bool | None = None
    parameter_controls_verified_clean: bool | None = None
    unauthorized_execution_detected: bool = False
    post_halt_execution_detected: bool = False
    parameter_bypass_detected: bool = False


class GovernorAuditEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    authorized_intents: int = Field(default=0, ge=0)
    rejected_intents: int = Field(default=0, ge=0)


class TrustEvidenceBundle(BaseModel):
    model_config = ConfigDict(frozen=True)

    cell_id: UUID
    closed_trades: tuple[ClosedTradeEvidence, ...] = ()
    equity_curve: tuple[EquityPoint, ...] = ()
    executions: tuple[ExecutionEvidence, ...] = ()
    safety: SafetyAuditEvidence = Field(default_factory=SafetyAuditEvidence)
    governor: GovernorAuditEvidence = Field(default_factory=GovernorAuditEvidence)

    @model_validator(mode="after")
    def chronological_facts(self) -> "TrustEvidenceBundle":
        if list(self.closed_trades) != sorted(
            self.closed_trades, key=lambda trade: (trade.closed_at, str(trade.trade_id))
        ):
            raise ValueError("closed trades must be chronological")
        if list(self.equity_curve) != sorted(
            self.equity_curve, key=lambda point: point.timestamp
        ):
            raise ValueError("equity curve must be chronological")
        return self


class FactorScore(BaseModel):
    model_config = ConfigDict(frozen=True)

    factor: str
    status: EvidenceStatus
    score: Decimal | None = Field(default=None, ge=0, le=100)
    weight: Decimal = Field(ge=0)
    evidence_count: int = Field(default=0, ge=0)
    reason: str | None = None

    @model_validator(mode="after")
    def score_matches_status(self) -> "FactorScore":
        if self.status is EvidenceStatus.AVAILABLE and self.score is None:
            raise ValueError("available factor evidence requires a score")
        if self.status is not EvidenceStatus.AVAILABLE and self.score is not None:
            raise ValueError("unavailable factor evidence cannot carry a score")
        return self


class TrustPolicySpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    factor_weights: dict[str, Decimal]
    required_factors: tuple[str, ...]
    promotion_thresholds: dict[str, Decimal]
    demotion_thresholds: dict[str, Decimal] = Field(default_factory=dict)


class TrustEvaluationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    evaluation_id: UUID
    cell_id: UUID
    policy_id: UUID
    policy_version: str
    eligibility_status: SafetyEligibility
    score: Decimal | None
    eligible_for_promotion: bool
    current_autonomy_tier: str
    recommended_autonomy_tier: str
    evidence_trade_count: int
    window_trade_count: int
    window_start: datetime | None
    window_end: datetime | None
    factors: tuple[FactorScore, ...]
    disqualifiers: tuple[str, ...]
    evidence_manifest_hash: str = Field(min_length=64, max_length=64)
