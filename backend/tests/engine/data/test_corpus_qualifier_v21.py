import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from app.domain.enums import OptionRight
from engine.data.corpus_qualifier import (
    CorpusQualificationManifest,
    PilotDecisionPoint,
    PilotWindow,
    QualificationMetrics,
    QualificationStatus,
)
from engine.data.corpus_qualifier_v21 import (
    POLICY_VERSION,
    acquisition_target_expirations,
    qualify_staged_v21,
)
from engine.data.option_enrollment import CanonicalResolutionAccounting
from engine.data.streaming_pilot import CanonicalJsonArrayWriter, staged_artifact
from engine.validation.models import (
    CanonicalOptionChainSnapshot,
    CanonicalOptionContractQuote,
)


NOW = datetime(2024, 1, 2, 15, 0, tzinfo=timezone.utc)
UNDERLYING_ID = UUID("2a9b0414-a678-5368-ab4c-d37d8720aba6")


def _v1() -> CorpusQualificationManifest:
    accounting = CanonicalResolutionAccounting(
        discovered_contracts_count=40,
        resolved_existing_contracts_count=0,
        newly_enrolled_contracts_count=40,
        resolved_contracts_count=40,
        rejected_contracts_count=0,
        rejected_contracts=(),
    )
    return CorpusQualificationManifest(
        qualification_manifest_id=UUID("2e37725f-669b-5175-b41d-33ab194ad8ba"),
        qualification_manifest_sha256="1" * 64,
        qualification_policy_version="CORPUS-QUALIFICATION-v1",
        provider_code="THETA_DATA",
        pilot_window=PilotWindow(
            start_session=date(2024, 1, 2),
            end_session=date(2024, 1, 2),
            total_calendar_sessions=1,
            rth_expected_minutes=390,
        ),
        metrics=QualificationMetrics(
            underlying_bar_completeness_pct=Decimal("100.00"),
            underlying_status=QualificationStatus.PASS,
            strategy_signal_count=2,
            decision_point_complete_evidence_count=0,
            decision_point_evidence_pct=Decimal("0.00"),
            decision_evidence_status=QualificationStatus.FAIL,
            causal_timestamp_violations_count=0,
            causal_status=QualificationStatus.PASS,
            canonical_contract_resolution_pct=Decimal("100.00"),
            resolution_status=QualificationStatus.PASS,
            resolution_accounting=accounting,
            assigned_fidelity_tier="TIER_2_TRADE_HISTORY",
            fidelity_status=QualificationStatus.REVIEW,
        ),
        overall_qualification_verdict=QualificationStatus.FAIL,
        raw_artifacts_manifest_sha256="2" * 64,
        normalized_dataset_manifest_sha256="3" * 64,
    )


def _contract(strike: int, right: OptionRight, ordinal: int):
    symbol = f"TQQQ240105{right.value[0]}{strike:08d}"
    return CanonicalOptionContractQuote(
        contract_instrument_id=UUID(int=ordinal + 1),
        underlying_instrument_id=UNDERLYING_ID,
        underlying_symbol="TQQQ",
        canonical_contract_symbol=symbol,
        expiration_date=date(2024, 1, 5),
        strike_price=Decimal(strike),
        option_right=right,
        contract_multiplier=Decimal("100"),
        listing_type="STANDARD",
        bid_price=Decimal("0.47") if strike == 50 else Decimal("0"),
        ask_price=Decimal("0.50"),
        bid_size=Decimal("0"),
        ask_size=Decimal("0"),
        volume=10 if strike == 50 else 0,
        open_interest=0,
        liquidity_verifiable=True,
    )


def _snapshot(timestamp: datetime, *, below: int):
    strikes = [50 - value for value in range(1, below + 1)] + [
        50 + value for value in range(1, 11)
    ]
    # Include the nearest eligible contract without turning it into an ATM requirement.
    strikes.append(50)
    contracts = []
    ordinal = 0
    for right in (OptionRight.CALL, OptionRight.PUT):
        for strike in strikes:
            contracts.append(_contract(strike, right, ordinal))
            ordinal += 1
    return CanonicalOptionChainSnapshot(
        underlying_instrument_id=UNDERLYING_ID,
        underlying_symbol="TQQQ",
        canonical_completed_at=timestamp,
        contracts=tuple(contracts),
    )


def test_acquisition_expirations_reproduce_nearest_tie_and_deduplication():
    session = date(2024, 1, 2)
    available = (
        date(2024, 1, 5),
        date(2024, 1, 12),
        date(2024, 1, 19),
        date(2024, 1, 26),
    )
    assert acquisition_target_expirations(available, session) == available
    assert acquisition_target_expirations(
        (date(2024, 1, 8), date(2024, 1, 10)), session
    ) == (date(2024, 1, 8), date(2024, 1, 10))


def test_v21_scores_envelope_but_keeps_candidate_availability_unscored(tmp_path):
    path = tmp_path / "options.json"
    with CanonicalJsonArrayWriter(path) as writer:
        writer.append(_snapshot(NOW, below=10))
        writer.append(_snapshot(NOW.replace(minute=1), below=9))
    artifact = staged_artifact(path, "application/json")
    decisions = tuple(
        PilotDecisionPoint(
            underlying_instrument_id=UNDERLYING_ID,
            symbol="TQQQ",
            signal_at=timestamp,
            underlying_spot=Decimal("50"),
        )
        for timestamp in (NOW, NOW.replace(minute=1))
    )
    kwargs = dict(
        option_snapshot_artifacts={"TQQQ": artifact},
        decision_points=decisions,
        v1_manifest=_v1(),
        stage_2_receipt_sha256="4" * 64,
        stage_2_plan_identity={"sha256": "5" * 64, "byte_count": 1},
    )
    first = qualify_staged_v21(**kwargs)
    second = qualify_staged_v21(**kwargs)

    scope = first.scored_acquisition_qualification["acquisition_envelope"]
    assert first.qualification_policy_version == POLICY_VERSION
    assert scope["combined"] == {
        "decision_count": 2,
        "complete_decision_count": 1,
        "incomplete_decision_count": 1,
        "completeness_percentage": Decimal("50.00"),
        "status": "FAIL",
        "failure_attribution": {"STRIKE_ENVELOPE_DEFICIT": 1},
    }
    assert first.strategy_001_diagnostic["eligible_candidate_decision_count"] == 2
    assert first.strategy_001_diagnostic["candidate_availability_percentage"] == Decimal("100.00")
    assert first.strategy_001_diagnostic["scoring_effect"] == "NONE"
    assert first.strategy_001_diagnostic["live_capital_authorization"] is False
    assert first.overall_qualification_verdict is QualificationStatus.FAIL
    assert first.canonical_bytes() == second.canonical_bytes()
    assert hashlib.sha256(first.canonical_bytes()).hexdigest() == hashlib.sha256(
        second.canonical_bytes()
    ).hexdigest()
