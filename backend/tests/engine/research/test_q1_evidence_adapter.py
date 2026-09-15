from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from engine.research.q1_evidence_adapter import (
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


# 1. CALL and PUT evidence both survive adaptation
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


# 2. Simulator still selects CALL according to frozen policy
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


# 3. No synthetic is_weekly=True
def test_no_synthetic_is_weekly_in_model_or_adapter() -> None:
    assert "is_weekly" not in OptionCandidate.model_fields
    candidate = adapt_option_candidate(
        _sample_raw_call(),
        observed_at=datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc),
        underlying_symbol="TQQQ",
    )
    assert not hasattr(candidate, "is_weekly")


# 4. DTE/expiration behavior remains Policy v1.1 conformant (0 <= DTE <= 5, zero-DTE precedence)
def test_dte_expiration_behavior_policy_v1_1_conformant() -> None:
    signal = _Signal(
        symbol="TQQQ",
        kind=SignalKind.BULLISH,
        signal_at=datetime(2024, 1, 2, 14, 31, tzinfo=timezone.utc),
        legal_execution_at=datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc),
        spot_close=Decimal("44.00"),
    )
    # Session date is 2024-01-02 (Eastern)
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
        _sample_raw_call(symbol="SIX-DTE", expiration="2024-01-08"),  # DTE = 6 > 5
        observed_at=signal.legal_execution_at,
        underlying_symbol="TQQQ",
    )

    # Zero-DTE precedence
    assert resolve_candidate(signal, (zero_dte, one_dte)).instrument_id == "ZERO-DTE"
    # When zero-DTE absent, nearest calendar expiration (one-DTE) selected
    assert resolve_candidate(signal, (one_dte,)).instrument_id == "ONE-DTE"
    # Six-DTE disqualified
    assert resolve_candidate(signal, (six_dte,)) is None


# 5. ask <= 0 fails closed
def test_ask_less_than_or_equal_zero_fails_closed() -> None:
    obs = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    with pytest.raises(EvidenceIntegrityError, match="non-positive ask"):
        adapt_option_candidate(_sample_raw_call(ask="0.00"), observed_at=obs, underlying_symbol="TQQQ")
    with pytest.raises(EvidenceIntegrityError, match="non-positive ask"):
        adapt_option_candidate(_sample_raw_call(ask="-0.10"), observed_at=obs, underlying_symbol="TQQQ")


# 6. Inverted quote fails closed
def test_inverted_quote_fails_closed() -> None:
    obs = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    with pytest.raises(EvidenceIntegrityError, match="inverted quote"):
        adapt_option_candidate(
            _sample_raw_call(bid="0.50", ask="0.40"), observed_at=obs, underlying_symbol="TQQQ"
        )


# 7. Negative interval Bid fails closed (and candidate bid)
def test_negative_bid_fails_closed() -> None:
    obs = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    with pytest.raises(EvidenceIntegrityError, match="negative bid"):
        adapt_option_candidate(
            _sample_raw_call(bid="-0.01", ask="0.40"), observed_at=obs, underlying_symbol="TQQQ"
        )
    with pytest.raises(EvidenceIntegrityError, match="negative bid"):
        adapt_option_interval(_sample_raw_call(bid="-0.01"), completed_at=obs)


# 8. Missing volume remains missing
def test_missing_volume_remains_missing() -> None:
    obs = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    raw = _sample_raw_call()
    raw.pop("volume")
    candidate = adapt_option_candidate(raw, observed_at=obs, underlying_symbol="TQQQ")
    assert candidate.volume is None

    raw_none = _sample_raw_call(volume=None)
    candidate_none = adapt_option_candidate(raw_none, observed_at=obs, underlying_symbol="TQQQ")
    assert candidate_none.volume is None


# 9. Observed zero volume remains zero
def test_observed_zero_volume_remains_zero() -> None:
    obs = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    cand_int = adapt_option_candidate(_sample_raw_call(volume=0), observed_at=obs, underlying_symbol="TQQQ")
    assert cand_int.volume == 0
    assert cand_int.volume is not None

    cand_str = adapt_option_candidate(_sample_raw_call(volume="0"), observed_at=obs, underlying_symbol="TQQQ")
    assert cand_str.volume == 0


# 10. Missing OI remains missing
def test_missing_oi_remains_missing() -> None:
    obs = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    raw = _sample_raw_call()
    raw.pop("open_interest")
    candidate = adapt_option_candidate(raw, observed_at=obs, underlying_symbol="TQQQ")
    assert candidate.open_interest is None

    raw_none = _sample_raw_call(open_interest=None)
    candidate_none = adapt_option_candidate(raw_none, observed_at=obs, underlying_symbol="TQQQ")
    assert candidate_none.open_interest is None


# 11. Observed zero OI remains zero
def test_observed_zero_oi_remains_zero() -> None:
    obs = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    cand_int = adapt_option_candidate(_sample_raw_call(open_interest=0), observed_at=obs, underlying_symbol="TQQQ")
    assert cand_int.open_interest == 0
    assert cand_int.open_interest is not None

    cand_str = adapt_option_candidate(_sample_raw_call(open_interest="0"), observed_at=obs, underlying_symbol="TQQQ")
    assert cand_str.open_interest == 0


# 12. Missing canonical identity fails closed
def test_missing_canonical_identity_fails_closed() -> None:
    obs = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    raw = _sample_raw_call()
    raw.pop("canonical_contract_symbol")
    raw["contract_instrument_id"] = "fb452276-a4e0-52df-852d-c7bd91528afc"

    with pytest.raises(EvidenceIntegrityError, match="canonical_contract_symbol"):
        adapt_option_candidate(raw, observed_at=obs, underlying_symbol="TQQQ")

    with pytest.raises(EvidenceIntegrityError, match="canonical_contract_symbol"):
        adapt_option_interval(raw, completed_at=obs)


# 13. Contract/snapshot symbol contradiction fails closed
def test_contract_snapshot_symbol_contradiction_fails_closed() -> None:
    obs = datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc)
    raw = _sample_raw_call(underlying="SQQQ")  # contradiction with snapshot "TQQQ"

    with pytest.raises(EvidenceIntegrityError, match="contradiction"):
        adapt_option_candidate(raw, observed_at=obs, underlying_symbol="TQQQ")


# 14. Absent required contracts field fails closed
def test_absent_required_contracts_field_fails_closed() -> None:
    raw_snapshot = {
        "canonical_completed_at": "2024-01-02T14:32:00Z",
        "underlying_symbol": "TQQQ",
        # missing "contracts" key
    }
    with pytest.raises(EvidenceIntegrityError, match="missing required 'contracts' field"):
        adapt_option_snapshot(raw_snapshot)


# 15. Valid empty contracts collection remains distinguishable
def test_valid_empty_contracts_collection_remains_distinguishable() -> None:
    raw_snapshot = {
        "canonical_completed_at": "2024-01-02T14:32:00Z",
        "underlying_symbol": "TQQQ",
        "contracts": [],
    }
    candidates, intervals = adapt_option_snapshot(raw_snapshot)
    assert candidates == ()
    assert intervals == ()


# 16. Evidence SHA mismatch fails closed
def test_evidence_sha_mismatch_fails_closed() -> None:
    with pytest.raises(EvidenceProvenanceError, match="SHA-256 mismatch"):
        verify_artifact_provenance(
            "TQQQ_BARS",
            sha256="0000000000000000000000000000000000000000000000000000000000000000",
        )


# 17. Record-count mismatch fails closed
def test_record_count_mismatch_fails_closed() -> None:
    with pytest.raises(EvidenceProvenanceError, match="record count mismatch"):
        verify_artifact_provenance(
            "TQQQ_BARS",
            sha256="ec847019da3b1c2666e913b5702bedc087087e00622228cc79cc097a7f512404",
            record_count=100,
        )


# 18. Deterministic sorting remains byte-stable
def test_deterministic_sorting_remains_byte_stable() -> None:
    raw_bars = [
        {
            "symbol": "TQQQ",
            "interval_start_at": "2024-01-02T14:31:00Z",
            "completed_at": "2024-01-02T14:32:00Z",
            "close": "49.39",
        },
        {
            "symbol": "SQQQ",
            "interval_start_at": "2024-01-02T14:30:00Z",
            "completed_at": "2024-01-02T14:31:00Z",
            "close": "12.50",
        },
        {
            "symbol": "TQQQ",
            "interval_start_at": "2024-01-02T14:30:00Z",
            "completed_at": "2024-01-02T14:31:00Z",
            "close": "49.35",
        },
    ]
    raw_snapshots = [
        {
            "canonical_completed_at": "2024-01-02T14:32:00Z",
            "underlying_symbol": "TQQQ",
            "contracts": [
                _sample_raw_call(symbol="TQQQ-B"),
                _sample_raw_call(symbol="TQQQ-A"),
            ],
        }
    ]

    sim_1 = build_simulation_input(raw_bars=raw_bars, raw_snapshots=raw_snapshots)
    # Permute order of raw inputs
    sim_2 = build_simulation_input(
        raw_bars=list(reversed(raw_bars)),
        raw_snapshots=raw_snapshots,
    )

    assert [b.symbol for b in sim_1.underlying_bars] == ["SQQQ", "TQQQ", "TQQQ"]
    assert [b.interval_start_at for b in sim_1.underlying_bars] == [
        b.interval_start_at for b in sim_2.underlying_bars
    ]
    assert [c.instrument_id for c in sim_1.option_candidates] == ["TQQQ-A", "TQQQ-B"]
    assert sim_1.underlying_bars == sim_2.underlying_bars
    assert sim_1.option_candidates == sim_2.option_candidates
    assert sim_1.option_intervals == sim_2.option_intervals


# 19. Policy v1.1 SHA remains unchanged
def test_policy_v1_1_sha_remains_unchanged() -> None:
    content = POLICY_PATH.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    assert digest == "eaccb5b21f992148f07f2547d6729beed6beead5ee38e24dc45500518bb02718"
    assert digest == POLICY_SHA256


# 20. Liquidity predicate with explicit missingness semantics (Policy v1.1)
def test_liquidity_predicate_with_missingness_semantics() -> None:
    signal = _Signal(
        symbol="TQQQ",
        kind=SignalKind.BULLISH,
        signal_at=datetime(2024, 1, 2, 14, 31, tzinfo=timezone.utc),
        legal_execution_at=datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc),
        spot_close=Decimal("44.00"),
    )
    # Both None -> disqualified
    cand_both_none = adapt_option_candidate(
        _sample_raw_call(symbol="NONE-BOTH", volume=None, open_interest=None, expiration="2024-01-02"),
        observed_at=signal.legal_execution_at,
        underlying_symbol="TQQQ",
    )
    assert resolve_candidate(signal, (cand_both_none,)) is None

    # Vol=15, OI=None -> qualifies via volume >= 10
    cand_vol_only = adapt_option_candidate(
        _sample_raw_call(symbol="VOL-ONLY", volume=15, open_interest=None, expiration="2024-01-02"),
        observed_at=signal.legal_execution_at,
        underlying_symbol="TQQQ",
    )
    assert resolve_candidate(signal, (cand_vol_only,)).instrument_id == "VOL-ONLY"

    # Vol=None, OI=60 -> qualifies via OI >= 50
    cand_oi_only = adapt_option_candidate(
        _sample_raw_call(symbol="OI-ONLY", volume=None, open_interest=60, expiration="2024-01-02"),
        observed_at=signal.legal_execution_at,
        underlying_symbol="TQQQ",
    )
    assert resolve_candidate(signal, (cand_oi_only,)).instrument_id == "OI-ONLY"

    # Vol=0, OI=None -> disqualified
    cand_zero_vol_none_oi = adapt_option_candidate(
        _sample_raw_call(symbol="ZERO-VOL-NONE-OI", volume=0, open_interest=None, expiration="2024-01-02"),
        observed_at=signal.legal_execution_at,
        underlying_symbol="TQQQ",
    )
    assert resolve_candidate(signal, (cand_zero_vol_none_oi,)) is None
