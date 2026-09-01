import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.historical import (
    HistoricalMarketArtifact,
    HistoricalMarketDataset,
    HistoricalMarketDatasetSymbol,
)
from app.db.models.scorecards import (
    HistoricalValidationConfidenceLedger,
    HistoricalValidationRegimeSlice,
    HistoricalValidationRun,
)


POLICY_VERSION = "CONFIDENCE-POLICY-7FACTOR-v2"
SUFFICIENT = "SUFFICIENT"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
TWO = Decimal("0.01")
YEAR_SECONDS = Decimal("31557600")


def _q(value: Decimal) -> Decimal:
    return max(Decimal("0"), min(Decimal("100"), value)).quantize(TWO, rounding=ROUND_HALF_UP)


def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


class FactorEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    score: Decimal | None
    evidence_status: str
    sample_count: int = Field(ge=0)
    reason_codes: tuple[str, ...] = ()


class ConfidenceEvidence(BaseModel):
    """Observed replay evidence; canonical identities and summary counts remain DB-owned."""

    model_config = ConfigDict(frozen=True)

    expected_observations: int = Field(ge=0)
    observed_observations: int = Field(ge=0)
    gap_count: int = Field(ge=0)
    lookahead_violation_count: int = Field(ge=0)
    rth_boundary_adherence_pct: Decimal = Field(ge=0, le=100)
    execution_observation_count: int = Field(ge=0)
    execution_quality_pct: Decimal = Field(ge=0, le=100)
    oos_session_count: int = Field(ge=0)
    in_sample_expectancy_usd: Decimal | None
    oos_expectancy_usd: Decimal | None
    largest_session_profit_usd: Decimal = Field(ge=0)
    context_evaluation_count: int = Field(ge=0)
    context_aligned_count: int = Field(ge=0)
    claimed_dataset_manifest_sha256: str
    claimed_scorecard_manifest_sha256: str
    claimed_artifact_sha256s: tuple[str, ...]

    @model_validator(mode="after")
    def reconcile_counts(self) -> "ConfidenceEvidence":
        if self.observed_observations + self.gap_count > self.expected_observations:
            raise ValueError("observed observations plus gaps cannot exceed expected observations")
        if self.context_aligned_count > self.context_evaluation_count:
            raise ValueError("aligned ContextGate observations cannot exceed evaluations")
        return self


class ConfidenceManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest_version: str = "CONFIDENCE-MANIFEST-v1"
    payload: dict[str, Any]
    confidence_manifest_sha256: str

    @classmethod
    def build(cls, payload: dict[str, Any]) -> "ConfidenceManifest":
        canonical = _canonical(payload)
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return cls(payload=canonical, confidence_manifest_sha256=hashlib.sha256(encoded).hexdigest())


class ConfidenceResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    confidence_ledger_id: UUID
    factors: dict[str, FactorEvaluation]
    hard_gates: dict[str, dict[str, Any]]
    composite_confidence_score: Decimal
    confidence_tier: str
    hard_gate_passed: bool
    gate_eligible: bool
    manifest: ConfidenceManifest


class EvidenceConfidenceEngine:
    """Seven epistemic factors plus six independent, fail-closed gates."""

    policy_version = POLICY_VERSION
    weights = {
        "sample_size": Decimal("0.15"),
        "regime_coverage": Decimal("0.15"),
        "data_completeness": Decimal("0.15"),
        "execution_realism": Decimal("0.15"),
        "oos_stability": Decimal("0.15"),
        "profit_distribution": Decimal("0.15"),
        "context_alignment": Decimal("0.10"),
    }
    fidelity_ceilings = {
        "TIER_1_QUOTE_DEPTH": Decimal("100.00"),
        "TIER_2_TRADE_HISTORY": Decimal("75.00"),
        "TIER_3_BAR_ONLY": Decimal("40.00"),
    }

    def __init__(self, session: Session) -> None:
        self.session = session

    def evaluate(
        self,
        *,
        validation_run_id: UUID,
        evidence: ConfidenceEvidence,
        evaluated_at: datetime,
    ) -> ConfidenceResult:
        run = self.session.get(HistoricalValidationRun, validation_run_id)
        if run is None:
            raise ValueError("validation run must resolve canonically")
        dataset = self.session.get(HistoricalMarketDataset, run.dataset_id)
        if dataset is None:
            raise ValueError("validation dataset must resolve canonically")
        if evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware")
        ceiling = self.fidelity_ceilings.get(dataset.liquidity_fidelity_tier)
        if ceiling is None:
            raise ValueError("unknown canonical liquidity fidelity tier")

        regimes = tuple(self.session.scalars(select(HistoricalValidationRegimeSlice).where(
            HistoricalValidationRegimeSlice.validation_run_id == validation_run_id
        )))
        years = Decimal(str((run.sample_end_time - run.sample_start_time).total_seconds())) / YEAR_SECONDS
        factors = self._factors(run, regimes, years, ceiling, evidence)
        crypto_passed, crypto_reasons = self._verify_lineage(run, dataset, evidence)
        hard_gates = self._hard_gates(run, years, dataset.liquidity_fidelity_tier, evidence, factors, crypto_passed, crypto_reasons)
        hard_gate_passed = all(bool(gate["passed"]) for gate in hard_gates.values())
        composite = _q(sum(
            (factor.score if factor.score is not None else Decimal("0")) * self.weights[name]
            for name, factor in factors.items()
        ))
        tier = "HIGH_CONFIDENCE" if composite >= 80 else "MODERATE_CONFIDENCE" if composite >= 65 else "LOW_CONFIDENCE"
        eligible = composite >= 80 and hard_gate_passed
        ledger_id = uuid5(NAMESPACE_URL, f"kairo:confidence:{validation_run_id}:{self.policy_version}")
        payload = {
            "confidence_ledger_id": ledger_id,
            "validation_run_id": validation_run_id,
            "confidence_policy_version": self.policy_version,
            "liquidity_fidelity_tier": dataset.liquidity_fidelity_tier,
            "factor_weights": self.weights,
            "factors": {name: factor.model_dump(mode="python") for name, factor in factors.items()},
            "hard_gates": hard_gates,
            "composite_confidence_score": composite,
            "confidence_tier": tier,
            "hard_gate_passed": hard_gate_passed,
            "gate_eligible": eligible,
            "evaluated_at": evaluated_at.astimezone(timezone.utc),
        }
        manifest = ConfidenceManifest.build(payload)
        existing = self.session.get(HistoricalValidationConfidenceLedger, ledger_id)
        if existing is not None:
            if existing.confidence_manifest_sha256 != manifest.confidence_manifest_sha256:
                raise ValueError("conflicting immutable confidence ledger identity")
            return ConfidenceResult(confidence_ledger_id=ledger_id, factors=factors, hard_gates=hard_gates, composite_confidence_score=composite, confidence_tier=tier, hard_gate_passed=hard_gate_passed, gate_eligible=eligible, manifest=manifest)

        values: dict[str, Any] = {}
        for name, factor in factors.items():
            values[f"{name}_score"] = factor.score
            values[f"{name}_status"] = factor.evidence_status
            values[f"{name}_count"] = factor.sample_count
            values[f"{name}_reasons"] = list(factor.reason_codes)
        self.session.add(HistoricalValidationConfidenceLedger(
            confidence_ledger_id=ledger_id,
            validation_run_id=validation_run_id,
            confidence_policy_version=self.policy_version,
            liquidity_fidelity_tier=dataset.liquidity_fidelity_tier,
            composite_confidence_score=composite,
            confidence_tier=tier,
            hard_gate_passed=hard_gate_passed,
            gate_eligible=eligible,
            hard_gate_evaluations_json=_canonical(hard_gates),
            confidence_manifest_sha256=manifest.confidence_manifest_sha256,
            evaluated_at=evaluated_at,
            **values,
        ))
        self.session.flush()
        return ConfidenceResult(confidence_ledger_id=ledger_id, factors=factors, hard_gates=hard_gates, composite_confidence_score=composite, confidence_tier=tier, hard_gate_passed=hard_gate_passed, gate_eligible=eligible, manifest=manifest)

    def _factors(self, run, regimes, years: Decimal, ceiling: Decimal, evidence: ConfidenceEvidence) -> dict[str, FactorEvaluation]:
        sample_ratios = (
            Decimal(run.total_sessions_count) / Decimal("500"),
            Decimal(run.total_trades_count) / Decimal("150"),
            years / Decimal("5"),
        )
        sample = FactorEvaluation(score=_q(sum(min(Decimal("1"), ratio) for ratio in sample_ratios) / Decimal("3") * 100), evidence_status=SUFFICIENT, sample_count=run.total_sessions_count)
        regime_count = len({row.regime_code for row in regimes})
        regime = FactorEvaluation(score=_q(Decimal(regime_count) / Decimal("7") * 100), evidence_status=SUFFICIENT if regime_count else INSUFFICIENT, sample_count=regime_count, reason_codes=() if regime_count else ("NO_REGIME_EVIDENCE",))
        if evidence.expected_observations == 0:
            completeness = FactorEvaluation(score=None, evidence_status=INSUFFICIENT, sample_count=0, reason_codes=("NO_EXPECTED_OBSERVATION_BASELINE",))
        else:
            observation_rate = Decimal("1") - Decimal(evidence.gap_count) / Decimal(evidence.expected_observations)
            reasons = ("CAUSAL_LOOKAHEAD_VIOLATION",) if evidence.lookahead_violation_count else ()
            completeness = FactorEvaluation(score=_q(observation_rate * 100), evidence_status=SUFFICIENT, sample_count=evidence.observed_observations, reason_codes=reasons)
        if evidence.execution_observation_count == 0:
            execution = FactorEvaluation(score=None, evidence_status=INSUFFICIENT, sample_count=0, reason_codes=("NO_EXECUTION_EVIDENCE",))
        else:
            capped = min(evidence.execution_quality_pct, ceiling)
            reasons = ("FIDELITY_TIER_CEILING_APPLIED",) if evidence.execution_quality_pct > ceiling else ()
            execution = FactorEvaluation(score=_q(capped), evidence_status=SUFFICIENT, sample_count=evidence.execution_observation_count, reason_codes=reasons)
        if evidence.in_sample_expectancy_usd is None or evidence.in_sample_expectancy_usd <= 0:
            oos = FactorEvaluation(score=Decimal("0.00"), evidence_status=INSUFFICIENT, sample_count=evidence.oos_session_count, reason_codes=("NON_POSITIVE_IN_SAMPLE_BASELINE",))
        elif evidence.oos_expectancy_usd is None or evidence.oos_session_count == 0:
            oos = FactorEvaluation(score=None, evidence_status=INSUFFICIENT, sample_count=evidence.oos_session_count, reason_codes=("MISSING_OOS_EXPECTANCY",))
        else:
            oos = FactorEvaluation(score=_q(evidence.oos_expectancy_usd / evidence.in_sample_expectancy_usd * 100), evidence_status=SUFFICIENT, sample_count=evidence.oos_session_count)
        gross_profit = Decimal(run.gross_profit_usd)
        if gross_profit <= 0:
            profit = FactorEvaluation(score=Decimal("0.00"), evidence_status=SUFFICIENT, sample_count=run.winning_trades_count, reason_codes=("ZERO_GROSS_PROFIT_BASELINE",))
        else:
            concentration = evidence.largest_session_profit_usd / gross_profit
            profit = FactorEvaluation(score=_q((Decimal("1") - concentration) * 100), evidence_status=SUFFICIENT, sample_count=run.winning_trades_count)
        if evidence.context_evaluation_count == 0:
            context = FactorEvaluation(score=None, evidence_status=INSUFFICIENT, sample_count=0, reason_codes=("NO_CONTEXT_GATE_EVIDENCE",))
        else:
            context = FactorEvaluation(score=_q(Decimal(evidence.context_aligned_count) / Decimal(evidence.context_evaluation_count) * 100), evidence_status=SUFFICIENT, sample_count=evidence.context_evaluation_count)
        return {"sample_size": sample, "regime_coverage": regime, "data_completeness": completeness, "execution_realism": execution, "oos_stability": oos, "profit_distribution": profit, "context_alignment": context}

    def _verify_lineage(self, run, dataset, evidence: ConfidenceEvidence) -> tuple[bool, tuple[str, ...]]:
        reasons: list[str] = []
        if not _valid_sha256(evidence.claimed_dataset_manifest_sha256) or evidence.claimed_dataset_manifest_sha256 != dataset.dataset_manifest_sha256:
            reasons.append("DATASET_MANIFEST_MISMATCH")
        if not _valid_sha256(evidence.claimed_scorecard_manifest_sha256) or evidence.claimed_scorecard_manifest_sha256 != run.scorecard_manifest_sha256:
            reasons.append("SCORECARD_MANIFEST_MISMATCH")
        symbols = tuple(self.session.scalars(select(HistoricalMarketDatasetSymbol).where(HistoricalMarketDatasetSymbol.dataset_id == dataset.dataset_id)))
        canonical_hashes: list[str] = []
        for symbol in symbols:
            raw = self.session.get(HistoricalMarketArtifact, symbol.raw_artifact_id)
            normalized = self.session.get(HistoricalMarketArtifact, symbol.normalized_artifact_id)
            if raw is None or normalized is None or raw.content_sha256 != symbol.raw_content_sha256 or normalized.content_sha256 != symbol.normalized_content_sha256:
                reasons.append("ARTIFACT_BINDING_MISMATCH")
                continue
            canonical_hashes.extend((raw.content_sha256, normalized.content_sha256))
        claimed = tuple(sorted(set(evidence.claimed_artifact_sha256s)))
        canonical = tuple(sorted(set(canonical_hashes)))
        if not canonical or any(not _valid_sha256(item) for item in claimed) or claimed != canonical:
            reasons.append("ARTIFACT_MANIFEST_MISMATCH")
        return not reasons, tuple(sorted(set(reasons)))

    @staticmethod
    def _gate(passed: bool, facts: dict[str, Any], reasons: tuple[str, ...] = ()) -> dict[str, Any]:
        return {"passed": passed, "facts": _canonical(facts), "reason_codes": list(reasons)}

    def _hard_gates(self, run, years: Decimal, tier: str, evidence: ConfidenceEvidence, factors: dict[str, FactorEvaluation], crypto_passed: bool, crypto_reasons: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        breadth = run.total_sessions_count >= 500 and run.total_trades_count >= 150 and years >= 5
        causal = evidence.lookahead_violation_count == 0 and evidence.rth_boundary_adherence_pct == 100
        fidelity = tier in ("TIER_1_QUOTE_DEPTH", "TIER_2_TRADE_HISTORY")
        oos = evidence.oos_session_count >= 250 and evidence.in_sample_expectancy_usd is not None and evidence.in_sample_expectancy_usd > 0
        factors_sufficient = all(factor.evidence_status == SUFFICIENT for factor in factors.values())
        return {
            "minimum_sample_breadth": self._gate(breadth, {"sessions": run.total_sessions_count, "trades": run.total_trades_count, "calendar_years": years}, () if breadth else ("MINIMUM_SAMPLE_BREADTH_NOT_MET",)),
            "causal_integrity": self._gate(causal, {"lookahead_violations": evidence.lookahead_violation_count, "rth_boundary_adherence_pct": evidence.rth_boundary_adherence_pct}, () if causal else ("CAUSAL_INTEGRITY_FAILED",)),
            "cryptographic_lineage": self._gate(crypto_passed, {"artifact_count": len(evidence.claimed_artifact_sha256s)}, crypto_reasons),
            "execution_fidelity_tier": self._gate(fidelity, {"liquidity_fidelity_tier": tier}, () if fidelity else ("INELIGIBLE_FIDELITY_TIER",)),
            "oos_evidence_sufficiency": self._gate(oos, {"oos_sessions": evidence.oos_session_count, "in_sample_expectancy_usd": evidence.in_sample_expectancy_usd}, () if oos else ("OOS_EVIDENCE_NOT_SUFFICIENT",)),
            "required_factor_sufficiency": self._gate(factors_sufficient, {"factor_statuses": {name: factor.evidence_status for name, factor in factors.items()}}, () if factors_sufficient else ("REQUIRED_FACTOR_EVIDENCE_MISSING",)),
        }
