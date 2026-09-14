"""Mechanically derived Strategy 001 corpus qualification policy v2.1.

Policy v1 remains in :mod:`engine.data.corpus_qualifier`.  This module evaluates
the frozen Q1 acquisition envelope independently and keeps Strategy 001 candidate
availability as a non-scoring diagnostic.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, model_validator

from app.domain.enums import OptionRight
from app.domain.instruments import CanonicalInstrument
from engine.data.corpus_qualifier import (
    CorpusQualificationEngine,
    CorpusQualificationManifest,
    PilotDecisionPoint,
    QualificationStatus,
)
from engine.data.streaming_pilot import iter_canonical_json_array
from engine.strategy.option_resolver import (
    LegacySessionExpirationResolver,
    MappingInstrumentLookup,
    OptionContractCandidate,
    resolve_legacy_option,
)
from engine.strategy.registry_seed import STRATEGY_ID, STRATEGY_VERSION
from engine.validation.feed_loader import StagedArtifact, canonical_json_bytes
from engine.validation.models import CanonicalOptionChainSnapshot
from engine.validation.session_calendar import SessionCalendarResolver


POLICY_VERSION = "CORPUS-QUALIFICATION-v2.1"
POLICY_RECONCILIATION_REASON = (
    "CONFIRMED_ACQUISITION_QUALIFICATION_SPECIFICATION_MISMATCH"
)
TARGET_DTES = (0, 1, 7, 14, 30)
STRIKES_EACH_SIDE = 10


class CorpusQualificationV21Manifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    qualification_manifest_id: UUID
    qualification_manifest_sha256: str
    qualification_policy_version: str
    provider_code: str
    stage_2_predecessor: dict[str, Any]
    v1_lineage: dict[str, Any]
    pilot_window: dict[str, Any]
    scored_acquisition_qualification: dict[str, Any]
    strategy_001_diagnostic: dict[str, Any]
    overall_qualification_verdict: QualificationStatus
    raw_artifacts_manifest_sha256: str
    normalized_dataset_manifest_sha256: str

    @model_validator(mode="after")
    def identity_is_canonical(self) -> "CorpusQualificationV21Manifest":
        body = self.model_dump(
            mode="json",
            exclude={"qualification_manifest_id", "qualification_manifest_sha256"},
        )
        digest = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
        expected_id = uuid5(NAMESPACE_URL, f"kairo:corpus-qualification:{digest}")
        if self.qualification_manifest_sha256 != digest:
            raise ValueError("v2.1 qualification manifest SHA-256 is invalid")
        if self.qualification_manifest_id != expected_id:
            raise ValueError("v2.1 qualification manifest UUID is invalid")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))


def acquisition_target_expirations(
    available: Sequence[date], session_date: date
) -> tuple[date, ...]:
    """Reproduce ThetaDataV3ClientTransport._target_expirations exactly."""
    causal = {expiration for expiration in available if expiration >= session_date}
    if not causal:
        return ()
    chosen = {
        min(
            causal,
            key=lambda expiry: (
                abs((expiry - session_date).days - target_dte),
                expiry,
            ),
        )
        for target_dte in TARGET_DTES
    }
    return tuple(sorted(chosen))


def _percentage(numerator: int, denominator: int) -> Decimal:
    return CorpusQualificationEngine._percentage(numerator, denominator)


def _status(value: Decimal) -> QualificationStatus:
    return CorpusQualificationEngine._threshold(
        value, Decimal("95.00"), Decimal("90.00")
    )


def _candidate(contract: Any) -> OptionContractCandidate:
    return OptionContractCandidate(
        instrument_id=contract.contract_instrument_id,
        underlying_symbol=contract.underlying_symbol,
        expiration_date=contract.expiration_date,
        strike_price=contract.strike_price,
        option_right=contract.option_right,
        contract_symbol=contract.canonical_contract_symbol,
        contract_multiplier=contract.contract_multiplier,
        listing_type=contract.listing_type,
        bid=contract.bid_price,
        ask=contract.ask_price,
        volume=contract.volume,
        open_interest=contract.open_interest,
    )


def _instrument(contract: Any) -> CanonicalInstrument:
    return CanonicalInstrument(
        instrument_id=contract.contract_instrument_id,
        symbol=contract.canonical_contract_symbol,
        asset_class="OPTION",
        currency="USD",
        underlying_symbol=contract.underlying_symbol,
        contract_symbol=contract.canonical_contract_symbol,
        expiration_date=contract.expiration_date,
        strike_price=contract.strike_price,
        option_right=contract.option_right,
        contract_multiplier=contract.contract_multiplier,
        listing_type=contract.listing_type,
        effective_from=datetime.min.replace(tzinfo=timezone.utc),
    )


def _strategy_availability(
    snapshot: CanonicalOptionChainSnapshot,
    decision: PilotDecisionPoint,
    resolver: LegacySessionExpirationResolver,
) -> tuple[bool, bool]:
    if not snapshot.contracts:
        return False, False
    expiration = resolver.resolve(
        snapshot.underlying_symbol,
        tuple(contract.expiration_date for contract in snapshot.contracts),
    )
    candidates = tuple(_candidate(contract) for contract in snapshot.contracts)
    lookup = MappingInstrumentLookup(
        tuple(_instrument(contract) for contract in snapshot.contracts)
    )
    available = []
    for right in (OptionRight.CALL, OptionRight.PUT):
        available.append(
            resolve_legacy_option(
                candidates=candidates,
                underlying_symbol=decision.symbol,
                expiration_date=expiration,
                option_right=right,
                spot_price=decision.underlying_spot,
                canonical_lookup=lookup,
            )
            is not None
        )
    return available[0], available[1]


def _scope_payload(
    *, total: int, complete: int, failures: Counter[str]
) -> dict[str, Any]:
    percentage = _percentage(complete, total)
    return {
        "decision_count": total,
        "complete_decision_count": complete,
        "incomplete_decision_count": total - complete,
        "completeness_percentage": percentage,
        "status": _status(percentage),
        "failure_attribution": dict(sorted(failures.items())),
    }


def qualify_staged_v21(
    *,
    option_snapshot_artifacts: Mapping[str, StagedArtifact],
    decision_points: Sequence[PilotDecisionPoint],
    v1_manifest: CorpusQualificationManifest,
    stage_2_receipt_sha256: str,
    stage_2_plan_identity: Mapping[str, Any],
    calendar: SessionCalendarResolver | None = None,
) -> CorpusQualificationV21Manifest:
    """Evaluate v2.1 from verified Stage 2 normalized streams in bounded memory."""
    calendar = calendar or SessionCalendarResolver()
    if v1_manifest.qualification_policy_version != "CORPUS-QUALIFICATION-v1":
        raise ValueError("v2.1 lineage requires the unchanged v1 qualification")
    if len(stage_2_receipt_sha256) != 64:
        raise ValueError("Stage 2 predecessor SHA-256 is invalid")
    plan_sha = str(stage_2_plan_identity.get("sha256", ""))
    if len(plan_sha) != 64:
        raise ValueError("Stage 2 plan identity is invalid")

    decisions_by_key = {
        (decision.underlying_instrument_id, decision.signal_at): decision
        for decision in decision_points
    }
    if len(decisions_by_key) != len(decision_points):
        raise ValueError("v2.1 decision points are not unique")
    failures_by_key: dict[tuple[UUID, datetime], set[str]] = {
        key: {"MISSING_DECISION_SNAPSHOT"} for key in decisions_by_key
    }
    diagnostics = defaultdict(
        lambda: {"call": 0, "put": 0, "either": 0, "resolver_failures": 0}
    )
    prior_by_symbol: dict[str, datetime] = {}
    session_resolvers: dict[tuple[str, date], LegacySessionExpirationResolver] = {}

    for symbol in sorted(option_snapshot_artifacts):
        artifact = option_snapshot_artifacts[symbol]
        for value in iter_canonical_json_array(artifact.path):
            snapshot = CanonicalOptionChainSnapshot.model_validate(value)
            if snapshot.underlying_symbol != symbol:
                raise ValueError("option stream contains the wrong underlying symbol")
            key = (snapshot.underlying_instrument_id, snapshot.canonical_completed_at)
            decision = decisions_by_key.get(key)
            if decision is None:
                continue
            observed_failures: set[str] = set()
            prior = prior_by_symbol.get(symbol)
            if prior is not None and snapshot.canonical_completed_at <= prior:
                observed_failures.add("CAUSAL_ORDER_VIOLATION")
            prior_by_symbol[symbol] = snapshot.canonical_completed_at
            if decision.symbol != symbol:
                observed_failures.add("SNAPSHOT_IDENTITY_MISMATCH")

            expirations = tuple(
                sorted({contract.expiration_date for contract in snapshot.contracts})
            )
            session_date = decision.signal_at.astimezone(calendar.eastern).date()
            if not expirations:
                observed_failures.add("NO_ACQUIRED_EXPIRATION")
            elif acquisition_target_expirations(expirations, session_date) != expirations:
                observed_failures.add("EXPIRATION_SELECTION_MISMATCH")

            strike_deficit = False
            for expiration in expirations:
                for right in (OptionRight.CALL, OptionRight.PUT):
                    strikes = {
                        contract.strike_price
                        for contract in snapshot.contracts
                        if contract.expiration_date == expiration
                        and contract.option_right is right
                    }
                    if (
                        sum(strike < decision.underlying_spot for strike in strikes)
                        < STRIKES_EACH_SIDE
                        or sum(strike > decision.underlying_spot for strike in strikes)
                        < STRIKES_EACH_SIDE
                    ):
                        strike_deficit = True
            if strike_deficit:
                observed_failures.add("STRIKE_ENVELOPE_DEFICIT")
            failures_by_key[key] = observed_failures

            resolver = session_resolvers.setdefault(
                (symbol, session_date),
                LegacySessionExpirationResolver(session_date=session_date),
            )
            try:
                call_available, put_available = _strategy_availability(
                    snapshot, decision, resolver
                )
            except (TypeError, ValueError):
                diagnostics[symbol]["resolver_failures"] += 1
            else:
                diagnostics[symbol]["call"] += int(call_available)
                diagnostics[symbol]["put"] += int(put_available)
                diagnostics[symbol]["either"] += int(call_available or put_available)

    per_symbol: dict[str, dict[str, Any]] = {}
    combined_failures: Counter[str] = Counter()
    combined_complete = 0
    for symbol in sorted(option_snapshot_artifacts):
        keys = [key for key, decision in decisions_by_key.items() if decision.symbol == symbol]
        complete = sum(not failures_by_key[key] for key in keys)
        failure_counts = Counter(
            failure for key in keys for failure in failures_by_key[key]
        )
        per_symbol[symbol] = _scope_payload(
            total=len(keys), complete=complete, failures=failure_counts
        )
        combined_complete += complete
        combined_failures.update(failure_counts)

    total = len(decision_points)
    combined = _scope_payload(
        total=total, complete=combined_complete, failures=combined_failures
    )
    available_by_symbol = {
        symbol: {
            "decision_count": per_symbol[symbol]["decision_count"],
            "call_candidate_available_count": diagnostics[symbol]["call"],
            "put_candidate_available_count": diagnostics[symbol]["put"],
            "either_right_candidate_available_count": diagnostics[symbol]["either"],
            "either_right_candidate_availability_percentage": _percentage(
                diagnostics[symbol]["either"], per_symbol[symbol]["decision_count"]
            ),
            "resolver_failure_count": diagnostics[symbol]["resolver_failures"],
        }
        for symbol in sorted(option_snapshot_artifacts)
    }
    available_total = sum(value["either"] for value in diagnostics.values())

    scored = {
        "acquisition_envelope": {"by_symbol": per_symbol, "combined": combined},
        "causal_integrity": {
            "violation_count": v1_manifest.metrics.causal_timestamp_violations_count,
            "status": v1_manifest.metrics.causal_status,
        },
        "contract_resolution": {
            "percentage": v1_manifest.metrics.canonical_contract_resolution_pct,
            "status": v1_manifest.metrics.resolution_status,
            "accounting": v1_manifest.metrics.resolution_accounting.model_dump(mode="json"),
        },
        "underlying_bars": {
            "completeness_percentage": v1_manifest.metrics.underlying_bar_completeness_pct,
            "status": v1_manifest.metrics.underlying_status,
        },
        "failure_attribution_limitation": {
            "exchange_listing_vs_collection": "UNRESOLVED_FROM_STAGE_2_NORMALIZED_EVIDENCE",
            "reason": (
                "Stage 2 normalized option snapshots do not retain the full contract-list universe"
            ),
        },
    }
    verdict_inputs = (
        combined["status"],
        v1_manifest.metrics.causal_status,
        v1_manifest.metrics.resolution_status,
        v1_manifest.metrics.underlying_status,
    )
    verdict = (
        QualificationStatus.FAIL
        if QualificationStatus.FAIL in verdict_inputs
        else QualificationStatus.REVIEW
        if QualificationStatus.REVIEW in verdict_inputs
        else QualificationStatus.PASS
    )
    diagnostic = {
        "scoring_effect": "NONE",
        "live_capital_authorization": False,
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "availability_definition": "AT_LEAST_ONE_ELIGIBLE_CALL_OR_PUT_ON_FROZEN_SESSION_EXPIRATION",
        "by_symbol": available_by_symbol,
        "eligible_candidate_decision_count": available_total,
        "decision_count": total,
        "candidate_availability_percentage": _percentage(available_total, total),
    }
    stage_2_predecessor = {
        "receipt_sha256": stage_2_receipt_sha256,
        "plan_object_identity": dict(stage_2_plan_identity),
    }
    v1_lineage = {
        "qualification_policy_version": v1_manifest.qualification_policy_version,
        "qualification_manifest_id": str(v1_manifest.qualification_manifest_id),
        "qualification_manifest_sha256": v1_manifest.qualification_manifest_sha256,
        "canonical_bytes_sha256": hashlib.sha256(v1_manifest.canonical_bytes()).hexdigest(),
        "reconciliation_reason": POLICY_RECONCILIATION_REASON,
    }
    body = {
        "qualification_policy_version": POLICY_VERSION,
        "provider_code": v1_manifest.provider_code,
        "stage_2_predecessor": stage_2_predecessor,
        "v1_lineage": v1_lineage,
        "pilot_window": v1_manifest.pilot_window.model_dump(mode="json"),
        "scored_acquisition_qualification": scored,
        "strategy_001_diagnostic": diagnostic,
        "overall_qualification_verdict": verdict,
        "raw_artifacts_manifest_sha256": v1_manifest.raw_artifacts_manifest_sha256,
        "normalized_dataset_manifest_sha256": v1_manifest.normalized_dataset_manifest_sha256,
    }
    digest = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    return CorpusQualificationV21Manifest(
        qualification_manifest_id=uuid5(
            NAMESPACE_URL, f"kairo:corpus-qualification:{digest}"
        ),
        qualification_manifest_sha256=digest,
        **body,
    )
