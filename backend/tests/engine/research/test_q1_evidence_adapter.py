from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from engine.research.q1_evidence_adapter import (
    REQUIRED_ARTIFACT_KEYS,
    SEALED_EVIDENCE_IDENTITIES,
    SIMULATOR_COMMIT_SHA,
    STAGE_4_MANIFEST_SHA256,
    EvidenceIntegrityError,
    EvidenceProvenanceError,
    adapt_option_candidate,
    adapt_option_interval,
    adapt_option_snapshot,
    adapt_underlying_bar,
    build_simulation_input,
    verify_all_q1_evidence_provenance,
    verify_artifact_provenance,
)
from engine.research.strategy_001_simulator import (
    POLICY_PATH,
    POLICY_SHA256,
    OptionCandidate,
    OptionInterval,
    ResearchSimulationInput,
    UnderlyingBar,
    _Signal,
    SignalKind,
    resolve_candidate,
)

ET = ZoneInfo("America/New_York")


def _sample_raw_call(
    *,
    symbol: str = "TQQQ240105C00044500",
    underlying: str = "TQQQ",
    expiration: str = "2024-01-05",
    strike: str = "44.5",
    bid: str = "0.38",
    ask: str = "0.40",
    volume: int | str | None = 10,
    open_interest: int | str | None = 50,
) -> dict:
    raw = {
        "canonical_contract_symbol": symbol,
        "underlying_symbol": underlying,
        "expiration_date": expiration,
        "option_right": "CALL",
        "strike_price": strike,
        "bid_price": bid,
        "ask_price": ask,
    }
    if volume is not None:
        raw["volume"] = volume
    if open_interest is not None:
        raw["open_interest"] = open_interest
    return raw


def _sample_raw_put(
    *,
    symbol: str = "TQQQ240105P00044500",
    underlying: str = "TQQQ",
    expiration: str = "2024-01-05",
    strike: str = "44.5",
    bid: str = "0.38",
    ask: str = "0.40",
    volume: int | str | None = 10,
    open_interest: int | str | None = 50,
) -> dict:
    raw = {
        "canonical_contract_symbol": symbol,
        "underlying_symbol": underlying,
        "expiration_date": expiration,
        "option_right": "PUT",
        "strike_price": strike,
        "bid_price": bid,
        "ask_price": ask,
    }
    if volume is not None:
        raw["volume"] = volume
    if open_interest is not None:
        raw["open_interest"] = open_interest
    return raw


def _create_synthetic_payloads() -> tuple[dict[str, bytes], dict[str, dict[str, Any]]]:
    """Create a minimal, synthetically complete 5-artifact bundle and matching identities."""
    manifest_bytes = json.dumps({
        "dataset": "q1-2024",
        "qualification_policy_version": "v2.1",
        "overall_qualification_verdict": "FAIL",
    }, sort_keys=True).encode("utf-8")

    tqqq_bars = [
        {
            "symbol": "TQQQ",
            "interval_start_at": "2024-01-02T14:30:00Z",
            "completed_at": "2024-01-02T14:31:00Z",
            "close": "49.35",
        },
        {
            "symbol": "TQQQ",
            "interval_start_at": "2024-01-02T14:31:00Z",
            "completed_at": "2024-01-02T14:32:00Z",
            "close": "49.39",
        },
    ]
    tqqq_bars_bytes = json.dumps(tqqq_bars, sort_keys=True).encode("utf-8")

    sqqq_bars = [
        {
            "symbol": "SQQQ",
            "interval_start_at": "2024-01-02T14:30:00Z",
            "completed_at": "2024-01-02T14:31:00Z",
            "close": "12.50",
        },
    ]
    sqqq_bars_bytes = json.dumps(sqqq_bars, sort_keys=True).encode("utf-8")

    tqqq_options = [
        {
            "canonical_completed_at": "2024-01-02T14:32:00Z",
            "underlying_symbol": "TQQQ",
            "contracts": [
                _sample_raw_call(symbol="TQQQ240105C00044500"),
                _sample_raw_put(symbol="TQQQ240105P00044500"),
            ],
        }
    ]
    tqqq_options_bytes = json.dumps(tqqq_options, sort_keys=True).encode("utf-8")

    sqqq_options = [
        {
            "canonical_completed_at": "2024-01-02T14:32:00Z",
            "underlying_symbol": "SQQQ",
            "contracts": [
                _sample_raw_call(symbol="SQQQ240105C00013000", underlying="SQQQ", strike="13.0"),
            ],
        }
    ]
    sqqq_options_bytes = json.dumps(sqqq_options, sort_keys=True).encode("utf-8")

    payloads = {
        "STAGE_4_MANIFEST": manifest_bytes,
        "TQQQ_BARS": tqqq_bars_bytes,
        "SQQQ_BARS": sqqq_bars_bytes,
        "TQQQ_OPTIONS": tqqq_options_bytes,
        "SQQQ_OPTIONS": sqqq_options_bytes,
    }

    identities = {
        "STAGE_4_MANIFEST": {
            "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "record_count": 1,
        },
        "TQQQ_BARS": {
            "sha256": hashlib.sha256(tqqq_bars_bytes).hexdigest(),
            "record_count": len(tqqq_bars),
        },
        "SQQQ_BARS": {
            "sha256": hashlib.sha256(sqqq_bars_bytes).hexdigest(),
            "record_count": len(sqqq_bars),
        },
        "TQQQ_OPTIONS": {
            "sha256": hashlib.sha256(tqqq_options_bytes).hexdigest(),
            "record_count": len(tqqq_options),
        },
        "SQQQ_OPTIONS": {
            "sha256": hashlib.sha256(sqqq_options_bytes).hexdigest(),
            "record_count": len(sqqq_options),
        },
    }

    return payloads, identities


# 1. Mandatory provenance bundle: omitted provenance raises TypeError
def test_omitted_provenance_raises_type_error() -> None:
    with pytest.raises(TypeError):
        build_simulation_input()  # type: ignore[call-arg]


# 2. Empty provenance bundle fails closed
def test_empty_provenance_bundle_fails_closed() -> None:
    with pytest.raises(EvidenceProvenanceError, match="missing required artifacts"):
        build_simulation_input(artifact_payloads={})


# 3. One missing artifact fails closed
def test_one_missing_artifact_fails_closed() -> None:
    payloads, _ = _create_synthetic_payloads()
    payloads.pop("SQQQ_OPTIONS")
    with pytest.raises(EvidenceProvenanceError, match="missing required artifacts"):
        build_simulation_input(artifact_payloads=payloads)


# 4. Partial bundle fails closed
def test_partial_bundle_fails_closed() -> None:
    payloads, _ = _create_synthetic_payloads()
    partial = {"STAGE_4_MANIFEST": payloads["STAGE_4_MANIFEST"]}
    with pytest.raises(EvidenceProvenanceError, match="missing required artifacts"):
        build_simulation_input(artifact_payloads=partial)


# 5. Duplicate artifact in sequence fails closed
def test_duplicate_artifact_fails_closed() -> None:
    payloads, _ = _create_synthetic_payloads()
    items = list(payloads.items()) + [("STAGE_4_MANIFEST", payloads["STAGE_4_MANIFEST"])]
    with pytest.raises(EvidenceProvenanceError, match="Duplicate artifact"):
        build_simulation_input(artifact_payloads=items)


# 6. Extra/unknown artifact fails closed
def test_extra_unknown_artifact_fails_closed() -> None:
    payloads, _ = _create_synthetic_payloads()
    payloads["UNKNOWN_EXTRA"] = b"extra payload"
    with pytest.raises(EvidenceProvenanceError, match="unknown/extra artifacts"):
        build_simulation_input(artifact_payloads=payloads)


# 7. Correct expected SHA supplied with altered actual bytes fails closed
def test_correct_expected_sha_with_altered_actual_bytes_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads, identities = _create_synthetic_payloads()
    monkeypatch.setattr("engine.research.q1_evidence_adapter.SEALED_EVIDENCE_IDENTITIES", identities)

    # Alter actual bytes for TQQQ_BARS by 1 byte while identities expect original SHA
    payloads["TQQQ_BARS"] = payloads["TQQQ_BARS"] + b" "
    with pytest.raises(EvidenceProvenanceError, match="TQQQ_BARS SHA-256 mismatch"):
        build_simulation_input(artifact_payloads=payloads)


# 8. Incorrect Stage 4 manifest bytes fails closed
def test_incorrect_stage_4_manifest_bytes_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads, identities = _create_synthetic_payloads()
    monkeypatch.setattr("engine.research.q1_evidence_adapter.SEALED_EVIDENCE_IDENTITIES", identities)

    payloads["STAGE_4_MANIFEST"] = b'{"tampered": true}'
    with pytest.raises(EvidenceProvenanceError, match="STAGE_4_MANIFEST SHA-256 mismatch"):
        build_simulation_input(artifact_payloads=payloads)


# 9. Incorrect bars SHA fails closed
def test_incorrect_bars_sha_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads, identities = _create_synthetic_payloads()
    monkeypatch.setattr("engine.research.q1_evidence_adapter.SEALED_EVIDENCE_IDENTITIES", identities)

    payloads["SQQQ_BARS"] = b"[]"
    with pytest.raises(EvidenceProvenanceError, match="SQQQ_BARS SHA-256 mismatch"):
        build_simulation_input(artifact_payloads=payloads)


# 10. Incorrect options SHA fails closed
def test_incorrect_options_sha_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads, identities = _create_synthetic_payloads()
    monkeypatch.setattr("engine.research.q1_evidence_adapter.SEALED_EVIDENCE_IDENTITIES", identities)

    payloads["TQQQ_OPTIONS"] = b"[]"
    with pytest.raises(EvidenceProvenanceError, match="TQQQ_OPTIONS SHA-256 mismatch"):
        build_simulation_input(artifact_payloads=payloads)


# 11. Incorrect bar count fails closed
def test_incorrect_bar_count_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads, identities = _create_synthetic_payloads()
    # Modify expected count so that even if SHA matches, count check fails
    tqqq_bars_tampered = [
        {
            "symbol": "TQQQ",
            "interval_start_at": "2024-01-02T14:30:00Z",
            "completed_at": "2024-01-02T14:31:00Z",
            "close": "49.35",
        }
    ]  # 1 bar instead of 2
    tampered_bytes = json.dumps(tqqq_bars_tampered, sort_keys=True).encode("utf-8")
    payloads["TQQQ_BARS"] = tampered_bytes
    identities["TQQQ_BARS"]["sha256"] = hashlib.sha256(tampered_bytes).hexdigest()
    # Expected count remains 2
    identities["TQQQ_BARS"]["record_count"] = 2

    monkeypatch.setattr("engine.research.q1_evidence_adapter.SEALED_EVIDENCE_IDENTITIES", identities)
    with pytest.raises(EvidenceProvenanceError, match="TQQQ_BARS record count mismatch: expected 2, got 1"):
        build_simulation_input(artifact_payloads=payloads)


# 12. Incorrect snapshot count fails closed
def test_incorrect_snapshot_count_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads, identities = _create_synthetic_payloads()
    empty_options: list[dict] = []
    empty_bytes = json.dumps(empty_options).encode("utf-8")
    payloads["TQQQ_OPTIONS"] = empty_bytes
    identities["TQQQ_OPTIONS"]["sha256"] = hashlib.sha256(empty_bytes).hexdigest()
    identities["TQQQ_OPTIONS"]["record_count"] = 1  # expects 1, got 0

    monkeypatch.setattr("engine.research.q1_evidence_adapter.SEALED_EVIDENCE_IDENTITIES", identities)
    with pytest.raises(EvidenceProvenanceError, match="TQQQ_OPTIONS record count mismatch: expected 1, got 0"):
        build_simulation_input(artifact_payloads=payloads)


# 13. Exact five-artifact canonical evidence passes
def test_exact_five_artifact_canonical_evidence_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads, identities = _create_synthetic_payloads()
    monkeypatch.setattr("engine.research.q1_evidence_adapter.SEALED_EVIDENCE_IDENTITIES", identities)

    sim_input = build_simulation_input(artifact_payloads=payloads)
    assert isinstance(sim_input, ResearchSimulationInput)
    assert sim_input.stage_4_manifest_sha256 == identities["STAGE_4_MANIFEST"]["sha256"]
    assert len(sim_input.underlying_bars) == 3  # 2 TQQQ + 1 SQQQ
    assert len(sim_input.option_candidates) == 3  # 2 TQQQ + 1 SQQQ


# 14. Adaptation occurs only after provenance succeeds
def test_adaptation_occurs_only_after_provenance_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads, identities = _create_synthetic_payloads()
    # Introduce bad close price in bar (which would trigger EvidenceIntegrityError during adaptation)
    bad_bar = [{
        "symbol": "TQQQ",
        "interval_start_at": "2024-01-02T14:30:00Z",
        "completed_at": "2024-01-02T14:31:00Z",
        "close": "-99.00",  # Unphysical close
    }]
    payloads["TQQQ_BARS"] = json.dumps(bad_bar).encode("utf-8")
    # But leave identities unchanged so that SHA mismatch triggers first!
    monkeypatch.setattr("engine.research.q1_evidence_adapter.SEALED_EVIDENCE_IDENTITIES", identities)

    # Must raise EvidenceProvenanceError (SHA mismatch), NEVER reaching representation adaptation!
    with pytest.raises(EvidenceProvenanceError, match="TQQQ_BARS SHA-256 mismatch"):
        build_simulation_input(artifact_payloads=payloads)


# 15. CALL and PUT evidence both survive adaptation
def test_call_and_put_evidence_both_survive_adaptation() -> None:
    observed_at = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    raw_snapshot = {
        "canonical_completed_at": "2024-01-02T14:32:00Z",
        "underlying_symbol": "TQQQ",
        "contracts": [
            _sample_raw_call(symbol="TQQQ-CALL"),
            _sample_raw_put(symbol="TQQQ-PUT"),
        ],
    }
    candidates, intervals = adapt_option_snapshot(raw_snapshot)

    assert len(candidates) == 2
    assert {c.right for c in candidates} == {"CALL", "PUT"}
    assert {c.instrument_id for c in candidates} == {"TQQQ-CALL", "TQQQ-PUT"}
    assert len(intervals) == 2


# 16. Simulator still selects CALL according to frozen policy
def test_simulator_still_selects_call_according_to_frozen_policy() -> None:
    signal = _Signal(
        symbol="TQQQ",
        kind=SignalKind.BULLISH,
        signal_at=datetime(2024, 1, 2, 14, 31, tzinfo=timezone.utc),
        legal_execution_at=datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc),
        spot_close=Decimal("44.00"),
    )
    call_candidate = adapt_option_candidate(
        _sample_raw_call(symbol="TQQQ-CALL", strike="44.5", expiration="2024-01-02"),
        observed_at=signal.legal_execution_at,
        underlying_symbol="TQQQ",
    )
    put_candidate = adapt_option_candidate(
        _sample_raw_put(symbol="TQQQ-PUT", strike="44.5", expiration="2024-01-02"),
        observed_at=signal.legal_execution_at,
        underlying_symbol="TQQQ",
    )

    # Both provided; only CALL is selected
    selected = resolve_candidate(signal, (call_candidate, put_candidate))
    assert selected is not None
    assert selected.instrument_id == "TQQQ-CALL"
    assert selected.right == "CALL"

    # Only PUT provided; returns None
    assert resolve_candidate(signal, (put_candidate,)) is None


# 17. No synthetic is_weekly=True
def test_no_synthetic_is_weekly_in_model_or_adapter() -> None:
    assert "is_weekly" not in OptionCandidate.model_fields
    candidate = adapt_option_candidate(
        _sample_raw_call(),
        observed_at=datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc),
        underlying_symbol="TQQQ",
    )
    assert not hasattr(candidate, "is_weekly")


# 18. DTE/expiration behavior remains Policy v1.1 conformant (0 <= DTE <= 5, zero-DTE precedence)
def test_dte_expiration_behavior_policy_v1_1_conformant() -> None:
    signal = _Signal(
        symbol="TQQQ",
        kind=SignalKind.BULLISH,
        signal_at=datetime(2024, 1, 2, 14, 31, tzinfo=timezone.utc),
        legal_execution_at=datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc),
        spot_close=Decimal("44.00"),
    )
    zero_dte = adapt_option_candidate(
        _sample_raw_call(symbol="ZERO-DTE", expiration="2024-01-02"),
        observed_at=signal.legal_execution_at,
        underlying_symbol="TQQQ",
    )
    one_dte = adapt_option_candidate(
        _sample_raw_call(symbol="ONE-DTE", expiration="2024-01-03"),
        observed_at=signal.legal_execution_at,
        underlying_symbol="TQQQ",
    )
    six_dte = adapt_option_candidate(
        _sample_raw_call(symbol="SIX-DTE", expiration="2024-01-08"),
        observed_at=signal.legal_execution_at,
        underlying_symbol="TQQQ",
    )

    assert resolve_candidate(signal, (zero_dte, one_dte)).instrument_id == "ZERO-DTE"
    assert resolve_candidate(signal, (one_dte,)).instrument_id == "ONE-DTE"
    assert resolve_candidate(signal, (six_dte,)) is None


# 19. ask <= 0 fails closed
def test_ask_less_than_or_equal_zero_fails_closed() -> None:
    obs = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    with pytest.raises(EvidenceIntegrityError, match="non-positive ask"):
        adapt_option_candidate(_sample_raw_call(ask="0.00"), observed_at=obs, underlying_symbol="TQQQ")
    with pytest.raises(EvidenceIntegrityError, match="non-positive ask"):
        adapt_option_candidate(_sample_raw_call(ask="-0.10"), observed_at=obs, underlying_symbol="TQQQ")


# 20. Inverted quote fails closed
def test_inverted_quote_fails_closed() -> None:
    obs = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    with pytest.raises(EvidenceIntegrityError, match="inverted quote"):
        adapt_option_candidate(
            _sample_raw_call(bid="0.50", ask="0.40"), observed_at=obs, underlying_symbol="TQQQ"
        )


# 21. Negative interval Bid fails closed (and candidate bid)
def test_negative_bid_fails_closed() -> None:
    obs = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    with pytest.raises(EvidenceIntegrityError, match="negative bid"):
        adapt_option_candidate(
            _sample_raw_call(bid="-0.01", ask="0.40"), observed_at=obs, underlying_symbol="TQQQ"
        )
    with pytest.raises(EvidenceIntegrityError, match="negative bid"):
        adapt_option_interval(_sample_raw_call(bid="-0.01"), completed_at=obs)


# 22. Missing volume remains missing
def test_missing_volume_remains_missing() -> None:
    obs = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    raw = _sample_raw_call()
    raw.pop("volume")
    candidate = adapt_option_candidate(raw, observed_at=obs, underlying_symbol="TQQQ")
    assert candidate.volume is None


# 23. Observed zero volume remains zero
def test_observed_zero_volume_remains_zero() -> None:
    obs = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    cand_int = adapt_option_candidate(_sample_raw_call(volume=0), observed_at=obs, underlying_symbol="TQQQ")
    assert cand_int.volume == 0


# 24. Missing OI remains missing
def test_missing_oi_remains_missing() -> None:
    obs = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    raw = _sample_raw_call()
    raw.pop("open_interest")
    candidate = adapt_option_candidate(raw, observed_at=obs, underlying_symbol="TQQQ")
    assert candidate.open_interest is None


# 25. Observed zero OI remains zero
def test_observed_zero_oi_remains_zero() -> None:
    obs = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    cand_int = adapt_option_candidate(_sample_raw_call(open_interest=0), observed_at=obs, underlying_symbol="TQQQ")
    assert cand_int.open_interest == 0


# 26. Missing canonical identity fails closed
def test_missing_canonical_identity_fails_closed() -> None:
    obs = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    raw = _sample_raw_call()
    raw.pop("canonical_contract_symbol")
    raw["contract_instrument_id"] = "fb452276-a4e0-52df-852d-c7bd91528afc"

    with pytest.raises(EvidenceIntegrityError, match="canonical_contract_symbol"):
        adapt_option_candidate(raw, observed_at=obs, underlying_symbol="TQQQ")

    with pytest.raises(EvidenceIntegrityError, match="canonical_contract_symbol"):
        adapt_option_interval(raw, completed_at=obs)


# 27. Contract/snapshot symbol contradiction fails closed
def test_contract_snapshot_symbol_contradiction_fails_closed() -> None:
    obs = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    raw = _sample_raw_call(underlying="SQQQ")

    with pytest.raises(EvidenceIntegrityError, match="contradiction"):
        adapt_option_candidate(raw, observed_at=obs, underlying_symbol="TQQQ")


# 28. Absent required contracts field fails closed
def test_absent_required_contracts_field_fails_closed() -> None:
    raw_snapshot = {
        "canonical_completed_at": "2024-01-02T14:32:00Z",
        "underlying_symbol": "TQQQ",
    }
    with pytest.raises(EvidenceIntegrityError, match="missing required 'contracts' field"):
        adapt_option_snapshot(raw_snapshot)


# 29. Valid empty contracts collection remains distinguishable
def test_valid_empty_contracts_collection_remains_distinguishable() -> None:
    raw_snapshot = {
        "canonical_completed_at": "2024-01-02T14:32:00Z",
        "underlying_symbol": "TQQQ",
        "contracts": [],
    }
    candidates, intervals = adapt_option_snapshot(raw_snapshot)
    assert candidates == ()
    assert intervals == ()


# 30. Evidence SHA mismatch fails closed in standalone verify
def test_evidence_sha_mismatch_fails_closed() -> None:
    with pytest.raises(EvidenceProvenanceError, match="SHA-256 mismatch"):
        verify_artifact_provenance(
            "TQQQ_BARS",
            sha256="0000000000000000000000000000000000000000000000000000000000000000",
            record_count=23790,
        )


# 31. Record-count mismatch fails closed in standalone verify
def test_record_count_mismatch_fails_closed() -> None:
    with pytest.raises(EvidenceProvenanceError, match="record count mismatch"):
        verify_artifact_provenance(
            "TQQQ_BARS",
            sha256="ec847019da3b1c2666e913b5702bedc087087e00622228cc79cc097a7f512404",
            record_count=100,
        )


# 32. Mandatory record-count: passing None fails closed for counted artifacts
def test_mandatory_record_count_fails_when_omitted() -> None:
    with pytest.raises(EvidenceProvenanceError, match="record count mismatch"):
        verify_artifact_provenance(
            "TQQQ_BARS",
            sha256="ec847019da3b1c2666e913b5702bedc087087e00622228cc79cc097a7f512404",
            record_count=None,
        )


# 33. Deterministic sorting remains byte-stable
def test_deterministic_sorting_remains_byte_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads_1, identities_1 = _create_synthetic_payloads()
    monkeypatch.setattr("engine.research.q1_evidence_adapter.SEALED_EVIDENCE_IDENTITIES", identities_1)

    sim_1 = build_simulation_input(artifact_payloads=payloads_1)
    sim_2 = build_simulation_input(artifact_payloads=payloads_1)

    assert sim_1.underlying_bars == sim_2.underlying_bars
    assert sim_1.option_candidates == sim_2.option_candidates
    assert sim_1.option_intervals == sim_2.option_intervals
    assert sim_1.stage_4_manifest_sha256 == sim_2.stage_4_manifest_sha256


# 34. Policy v1.1 SHA remains unchanged
def test_policy_v1_1_sha_remains_unchanged() -> None:
    content = POLICY_PATH.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    assert digest == "eaccb5b21f992148f07f2547d6729beed6beead5ee38e24dc45500518bb02718"
    assert digest == POLICY_SHA256


# 35. Liquidity predicate with explicit missingness semantics (Policy v1.1)
def test_liquidity_predicate_with_missingness_semantics() -> None:
    signal = _Signal(
        symbol="TQQQ",
        kind=SignalKind.BULLISH,
        signal_at=datetime(2024, 1, 2, 14, 31, tzinfo=timezone.utc),
        legal_execution_at=datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc),
        spot_close=Decimal("44.00"),
    )
    cand_both_none = adapt_option_candidate(
        _sample_raw_call(symbol="NONE-BOTH", volume=None, open_interest=None, expiration="2024-01-02"),
        observed_at=signal.legal_execution_at,
        underlying_symbol="TQQQ",
    )
    assert resolve_candidate(signal, (cand_both_none,)) is None

    cand_vol_only = adapt_option_candidate(
        _sample_raw_call(symbol="VOL-ONLY", volume=15, open_interest=None, expiration="2024-01-02"),
        observed_at=signal.legal_execution_at,
        underlying_symbol="TQQQ",
    )
    assert resolve_candidate(signal, (cand_vol_only,)).instrument_id == "VOL-ONLY"

    cand_oi_only = adapt_option_candidate(
        _sample_raw_call(symbol="OI-ONLY", volume=None, open_interest=60, expiration="2024-01-02"),
        observed_at=signal.legal_execution_at,
        underlying_symbol="TQQQ",
    )
    assert resolve_candidate(signal, (cand_oi_only,)).instrument_id == "OI-ONLY"

    cand_zero_vol_none_oi = adapt_option_candidate(
        _sample_raw_call(symbol="ZERO-VOL-NONE-OI", volume=0, open_interest=None, expiration="2024-01-02"),
        observed_at=signal.legal_execution_at,
        underlying_symbol="TQQQ",
    )
    assert resolve_candidate(signal, (cand_zero_vol_none_oi,)) is None
