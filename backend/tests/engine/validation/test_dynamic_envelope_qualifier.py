"""Frozen black-box contract tests for DYNAMIC-ENVELOPE-CONTRACT-v1.0.

This module intentionally contains no qualification or slicing algorithm.  Each test
defines an externally observable input/output vector and exercises a strict Protocol
mock.  A production qualifier must implement the same Protocol and independently
satisfy these vectors; it must not import or reuse this test module as an oracle.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Protocol
from unittest.mock import create_autospec

import pytest


FROZEN_CONTRACT_ID = "DYNAMIC-ENVELOPE-CONTRACT-v1.0"
COMPLETED_AT = datetime(2024, 1, 2, 15, 30, tzinfo=timezone.utc)
ReasonCode = Literal[
    "PROVIDER_STRIKE_SET_UNAVAILABLE",
    "PROVIDER_WING_INSUFFICIENT",
    "DYNAMIC_ENVELOPE_DEFICIT",
    "EXPECTED_STRIKE_MISSING",
    "EXPECTED_CONTRACT_MISSING",
    "EXPECTED_QUOTE_MISSING",
    "QUOTE_INVERSION",
    "INVALID_ASK",
    "INVALID_BID",
    "TIMESTAMP_ALIGNMENT_FAILURE",
]
IntervalStatus = Literal["ENVELOPE_SATISFIED", "DYNAMIC_ENVELOPE_DEFICIT"]
CorpusStatus = Literal["PASS", "MARGINAL_REVIEW", "FAIL"]
OptionRight = Literal["CALL", "PUT"]


@dataclass(frozen=True)
class ExpectedContract:
    """Provider-listed identity; quote presence is independently observable."""

    contract_id: str
    strike: Decimal
    right: OptionRight
    quote_expected: bool = True


@dataclass(frozen=True)
class AcquiredContract:
    """Captured evidence; None means the required quote payload is absent."""

    contract_id: str
    strike: Decimal
    right: OptionRight
    bid: Decimal | None
    ask: Decimal | None


@dataclass(frozen=True)
class ExpectedUniverse:
    """Authoritative provider listing, distinct from the slicer's payload."""

    timestamp: datetime
    strikes: tuple[Decimal, ...]
    contracts: tuple[ExpectedContract, ...]


@dataclass(frozen=True)
class AcquiredUniverse:
    """Observed slicer payload, never a source of expected identities."""

    timestamp: datetime
    contracts: tuple[AcquiredContract, ...]


@dataclass(frozen=True)
class IntervalQualificationRequest:
    contract_id: str
    underlying_completed_at: datetime
    spot: Decimal
    expected_universe: ExpectedUniverse
    acquired_universe: AcquiredUniverse


@dataclass(frozen=True)
class IntervalQualificationResult:
    status: IntervalStatus
    atm_strike: Decimal | None
    required_strikes: tuple[Decimal, ...]
    reason_codes: tuple[ReasonCode, ...]


@dataclass(frozen=True)
class CorpusQualificationRequest:
    contract_id: str
    expected_interval_count: int
    interval_results: tuple[IntervalQualificationResult | None, ...]


@dataclass(frozen=True)
class IntegrityCounters:
    expected_intervals: int
    evaluated_intervals: int
    satisfied_intervals: int
    failed_intervals: int
    completeness_pct: Decimal
    provider_strike_set_failures: int
    provider_wing_insufficiencies: int
    expected_strike_omissions: int
    expected_contract_omissions: int
    missing_quotes: int
    quote_inversions: int
    invalid_asks: int
    invalid_bids: int
    timestamp_alignment_failures: int


@dataclass(frozen=True)
class CorpusQualificationResult:
    status: CorpusStatus
    counters: IntegrityCounters


class DynamicEnvelopeQualifier(Protocol):
    """Black-box interface the future production qualifier must satisfy."""

    def qualify_interval(
        self, request: IntervalQualificationRequest
    ) -> IntervalQualificationResult: ...

    def qualify_corpus(self, request: CorpusQualificationRequest) -> CorpusQualificationResult: ...


def _protocol_mock():
    """Return a strict interface mock; it performs no qualification work."""

    return create_autospec(DynamicEnvelopeQualifier, instance=True, spec_set=True)


def _strikes(*values: str | int) -> tuple[Decimal, ...]:
    return tuple(Decimal(str(value)) for value in values)


def _listed_contracts(strikes: tuple[Decimal, ...]) -> tuple[ExpectedContract, ...]:
    return tuple(
        ExpectedContract(
            contract_id=f"TQQQ-20240105-{right}-{strike}",
            strike=strike,
            right=right,
        )
        for strike in strikes
        for right in ("CALL", "PUT")
    )


def _acquired_contracts(
    contracts: tuple[ExpectedContract, ...],
    *,
    bid: Decimal = Decimal("1.00"),
    ask: Decimal = Decimal("1.05"),
) -> tuple[AcquiredContract, ...]:
    return tuple(
        AcquiredContract(
            contract_id=contract.contract_id,
            strike=contract.strike,
            right=contract.right,
            bid=bid,
            ask=ask,
        )
        for contract in contracts
    )


def _request(
    provider_strikes: tuple[Decimal, ...],
    *,
    spot: Decimal,
    expected_contracts: tuple[ExpectedContract, ...] | None = None,
    acquired_contracts: tuple[AcquiredContract, ...] | None = None,
    snapshot_timestamp: datetime = COMPLETED_AT,
) -> IntervalQualificationRequest:
    listed = expected_contracts if expected_contracts is not None else _listed_contracts(
        provider_strikes
    )
    acquired = (
        acquired_contracts
        if acquired_contracts is not None
        else _acquired_contracts(listed)
    )
    return IntervalQualificationRequest(
        contract_id=FROZEN_CONTRACT_ID,
        underlying_completed_at=COMPLETED_AT,
        spot=spot,
        expected_universe=ExpectedUniverse(
            timestamp=COMPLETED_AT,
            strikes=provider_strikes,
            contracts=listed,
        ),
        acquired_universe=AcquiredUniverse(
            timestamp=snapshot_timestamp,
            contracts=acquired,
        ),
    )


def _assert_interval_vector(
    request: IntervalQualificationRequest,
    expected: IntervalQualificationResult,
) -> None:
    qualifier = _protocol_mock()
    qualifier.qualify_interval.return_value = expected

    assert qualifier.qualify_interval(request) == expected
    qualifier.qualify_interval.assert_called_once_with(request)


def _assert_corpus_vector(
    request: CorpusQualificationRequest,
    expected: CorpusQualificationResult,
) -> None:
    qualifier = _protocol_mock()
    qualifier.qualify_corpus.return_value = expected

    assert qualifier.qualify_corpus(request) == expected
    qualifier.qualify_corpus.assert_called_once_with(request)


def _result(
    status: IntervalStatus,
    *,
    atm: Decimal | None,
    required: tuple[Decimal, ...] = (),
    reasons: tuple[ReasonCode, ...] = (),
) -> IntervalQualificationResult:
    return IntervalQualificationResult(
        status=status,
        atm_strike=atm,
        required_strikes=required,
        reason_codes=reasons,
    )


UNIFORM_STRIKES = tuple(Decimal(value) for value in range(90, 111))
NON_UNIFORM_STRIKES = _strikes(
    94,
    95,
    96,
    97,
    98,
    "98.50",
    99,
    "99.25",
    "99.50",
    "99.75",
    100,
    "100.50",
    101,
    "101.50",
    102,
    103,
    104,
    105,
    106,
    108,
    110,
)


def test_inputs_keep_expected_and_acquired_universes_independent():
    listed = _listed_contracts(UNIFORM_STRIKES)
    request = _request(
        UNIFORM_STRIKES,
        spot=Decimal("100"),
        expected_contracts=listed,
        acquired_contracts=_acquired_contracts(listed[:-2]),
    )

    assert isinstance(request.expected_universe, ExpectedUniverse)
    assert isinstance(request.acquired_universe, AcquiredUniverse)
    assert request.expected_universe.contracts is not request.acquired_universe.contracts
    assert len(request.expected_universe.contracts) == 42
    assert len(request.acquired_universe.contracts) == 40


def test_missing_expected_strike_fails_closed_against_independent_provider_universe():
    listed = _listed_contracts(UNIFORM_STRIKES)
    acquired = tuple(
        contract
        for contract in _acquired_contracts(listed)
        if contract.strike != Decimal("96")
    )
    request = _request(
        UNIFORM_STRIKES,
        spot=Decimal("100"),
        expected_contracts=listed,
        acquired_contracts=acquired,
    )
    expected = _result(
        "DYNAMIC_ENVELOPE_DEFICIT",
        atm=Decimal("100"),
        required=UNIFORM_STRIKES,
        reasons=("DYNAMIC_ENVELOPE_DEFICIT", "EXPECTED_STRIKE_MISSING"),
    )

    _assert_interval_vector(request, expected)


def test_atm_tie_resolves_to_lower_listed_strike_and_uses_ordinal_window():
    provider_strikes = tuple(Decimal(value) for value in range(90, 112))
    required = tuple(Decimal(value) for value in range(90, 111))
    request = _request(provider_strikes, spot=Decimal("100.50"))
    expected = _result("ENVELOPE_SATISFIED", atm=Decimal("100"), required=required)

    _assert_interval_vector(request, expected)


def test_non_uniform_provider_steps_pass_when_all_21_ordinal_strikes_are_present():
    request = _request(NON_UNIFORM_STRIKES, spot=Decimal("100.10"))
    expected = _result(
        "ENVELOPE_SATISFIED",
        atm=Decimal("100"),
        required=NON_UNIFORM_STRIKES,
    )

    _assert_interval_vector(request, expected)


@pytest.mark.parametrize(
    ("provider_strikes", "spot"),
    [
        (tuple(Decimal(value) for value in range(91, 111)), Decimal("100")),
        (tuple(Decimal(value) for value in range(90, 110)), Decimal("100")),
    ],
    ids=("nine-below", "nine-above"),
)
def test_provider_wing_with_fewer_than_ten_listed_strikes_fails(
    provider_strikes: tuple[Decimal, ...], spot: Decimal
):
    request = _request(provider_strikes, spot=spot)
    expected = _result(
        "DYNAMIC_ENVELOPE_DEFICIT",
        atm=Decimal("100"),
        reasons=("PROVIDER_WING_INSUFFICIENT",),
    )

    _assert_interval_vector(request, expected)


def test_unavailable_provider_strike_set_fails_closed():
    request = _request((), spot=Decimal("100"))
    expected = _result(
        "DYNAMIC_ENVELOPE_DEFICIT",
        atm=None,
        reasons=("PROVIDER_STRIKE_SET_UNAVAILABLE",),
    )

    _assert_interval_vector(request, expected)


def test_both_provider_listed_rights_are_required_at_every_envelope_strike():
    listed = _listed_contracts(UNIFORM_STRIKES)
    acquired = tuple(
        contract
        for contract in _acquired_contracts(listed)
        if not (contract.strike == Decimal("101") and contract.right == "PUT")
    )
    request = _request(
        UNIFORM_STRIKES,
        spot=Decimal("100"),
        expected_contracts=listed,
        acquired_contracts=acquired,
    )
    expected = _result(
        "DYNAMIC_ENVELOPE_DEFICIT",
        atm=Decimal("100"),
        required=UNIFORM_STRIKES,
        reasons=("EXPECTED_CONTRACT_MISSING",),
    )

    assert {contract.right for contract in request.expected_universe.contracts} == {
        "CALL",
        "PUT",
    }
    _assert_interval_vector(request, expected)


@pytest.mark.parametrize(
    ("bid", "ask", "reason"),
    [
        (Decimal("1.10"), Decimal("1.00"), "QUOTE_INVERSION"),
        (Decimal("0.00"), Decimal("0.00"), "INVALID_ASK"),
        (Decimal("0.00"), Decimal("-0.01"), "INVALID_ASK"),
        (Decimal("-0.01"), Decimal("1.00"), "INVALID_BID"),
    ],
    ids=("inverted", "zero-ask", "negative-ask", "negative-bid"),
)
def test_corrupt_required_quote_fails_without_silent_cleansing(
    bid: Decimal, ask: Decimal, reason: ReasonCode
):
    listed = _listed_contracts(UNIFORM_STRIKES)
    acquired = list(_acquired_contracts(listed))
    corrupt = acquired[20]
    acquired[20] = AcquiredContract(
        contract_id=corrupt.contract_id,
        strike=corrupt.strike,
        right=corrupt.right,
        bid=bid,
        ask=ask,
    )
    request = _request(
        UNIFORM_STRIKES,
        spot=Decimal("100"),
        expected_contracts=listed,
        acquired_contracts=tuple(acquired),
    )
    expected = _result(
        "DYNAMIC_ENVELOPE_DEFICIT",
        atm=Decimal("100"),
        required=UNIFORM_STRIKES,
        reasons=(reason,),
    )

    assert len(request.acquired_universe.contracts) == 42
    assert request.acquired_universe.contracts[20].bid == bid
    assert request.acquired_universe.contracts[20].ask == ask
    _assert_interval_vector(request, expected)


def test_missing_required_quote_payload_fails_closed():
    listed = _listed_contracts(UNIFORM_STRIKES)
    acquired = list(_acquired_contracts(listed))
    missing = acquired[20]
    acquired[20] = AcquiredContract(
        contract_id=missing.contract_id,
        strike=missing.strike,
        right=missing.right,
        bid=None,
        ask=None,
    )
    request = _request(
        UNIFORM_STRIKES,
        spot=Decimal("100"),
        expected_contracts=listed,
        acquired_contracts=tuple(acquired),
    )
    expected = _result(
        "DYNAMIC_ENVELOPE_DEFICIT",
        atm=Decimal("100"),
        required=UNIFORM_STRIKES,
        reasons=("EXPECTED_QUOTE_MISSING",),
    )

    _assert_interval_vector(request, expected)


def test_zero_bid_is_preserved_as_valid_evidence():
    listed = _listed_contracts(UNIFORM_STRIKES)
    request = _request(
        UNIFORM_STRIKES,
        spot=Decimal("100"),
        expected_contracts=listed,
        acquired_contracts=_acquired_contracts(
            listed,
            bid=Decimal("0.00"),
            ask=Decimal("0.05"),
        ),
    )
    expected = _result(
        "ENVELOPE_SATISFIED",
        atm=Decimal("100"),
        required=UNIFORM_STRIKES,
    )

    assert all(contract.bid == Decimal("0.00") for contract in request.acquired_universe.contracts)
    _assert_interval_vector(request, expected)


@pytest.mark.parametrize(
    "drift",
    [timedelta(microseconds=1), timedelta(seconds=30), timedelta(minutes=1)],
    ids=("sub-second-drift", "same-minute-drift", "nearest-neighbor-minute"),
)
def test_any_snapshot_timestamp_drift_fails_without_nearest_neighbor_substitution(
    drift: timedelta,
):
    request = _request(
        UNIFORM_STRIKES,
        spot=Decimal("100"),
        snapshot_timestamp=COMPLETED_AT + drift,
    )
    expected = _result(
        "DYNAMIC_ENVELOPE_DEFICIT",
        atm=Decimal("100"),
        required=UNIFORM_STRIKES,
        reasons=("TIMESTAMP_ALIGNMENT_FAILURE",),
    )

    _assert_interval_vector(request, expected)


def _corpus_result(
    status: CorpusStatus,
    *,
    expected_intervals: int,
    evaluated_intervals: int,
    satisfied_intervals: int,
    completeness_pct: str,
    failed_intervals: int | None = None,
    provider_strike_set_failures: int = 0,
    provider_wing_insufficiencies: int = 0,
    expected_strike_omissions: int = 0,
    expected_contract_omissions: int = 0,
    missing_quotes: int = 0,
    quote_inversions: int = 0,
    invalid_asks: int = 0,
    invalid_bids: int = 0,
    timestamp_alignment_failures: int = 0,
) -> CorpusQualificationResult:
    return CorpusQualificationResult(
        status=status,
        counters=IntegrityCounters(
            expected_intervals=expected_intervals,
            evaluated_intervals=evaluated_intervals,
            satisfied_intervals=satisfied_intervals,
            failed_intervals=(
                expected_intervals - satisfied_intervals
                if failed_intervals is None
                else failed_intervals
            ),
            completeness_pct=Decimal(completeness_pct),
            provider_strike_set_failures=provider_strike_set_failures,
            provider_wing_insufficiencies=provider_wing_insufficiencies,
            expected_strike_omissions=expected_strike_omissions,
            expected_contract_omissions=expected_contract_omissions,
            missing_quotes=missing_quotes,
            quote_inversions=quote_inversions,
            invalid_asks=invalid_asks,
            invalid_bids=invalid_bids,
            timestamp_alignment_failures=timestamp_alignment_failures,
        ),
    )


def test_missing_regular_session_interval_remains_in_390_interval_denominator():
    satisfied = _result(
        "ENVELOPE_SATISFIED",
        atm=Decimal("100"),
        required=UNIFORM_STRIKES,
    )
    request = CorpusQualificationRequest(
        contract_id=FROZEN_CONTRACT_ID,
        expected_interval_count=390,
        interval_results=(satisfied,) * 389 + (None,),
    )
    expected = _corpus_result(
        "PASS",
        expected_intervals=390,
        evaluated_intervals=389,
        satisfied_intervals=389,
        failed_intervals=1,
        completeness_pct="99.74",
    )

    _assert_corpus_vector(request, expected)


def test_failed_provider_retrieval_and_corrupt_interval_cannot_reduce_denominator():
    provider_failure = _result(
        "DYNAMIC_ENVELOPE_DEFICIT",
        atm=None,
        reasons=("PROVIDER_STRIKE_SET_UNAVAILABLE",),
    )
    corrupt = _result(
        "DYNAMIC_ENVELOPE_DEFICIT",
        atm=Decimal("100"),
        required=UNIFORM_STRIKES,
        reasons=("QUOTE_INVERSION",),
    )
    request = CorpusQualificationRequest(
        contract_id=FROZEN_CONTRACT_ID,
        expected_interval_count=390,
        interval_results=(None, provider_failure, corrupt),
    )
    expected = _corpus_result(
        "FAIL",
        expected_intervals=390,
        evaluated_intervals=2,
        satisfied_intervals=0,
        failed_intervals=390,
        completeness_pct="0.00",
        provider_strike_set_failures=1,
        quote_inversions=1,
    )

    _assert_corpus_vector(request, expected)


@pytest.mark.parametrize(
    ("satisfied", "expected_count", "percentage", "status"),
    [
        (9500, 10000, "95.00", "PASS"),
        (9499, 10000, "94.99", "MARGINAL_REVIEW"),
        (9000, 10000, "90.00", "MARGINAL_REVIEW"),
        (8999, 10000, "89.99", "FAIL"),
    ],
    ids=("pass-95.00", "marginal-94.99", "marginal-90.00", "fail-89.99"),
)
def test_exact_corpus_qualification_boundaries(
    satisfied: int,
    expected_count: int,
    percentage: str,
    status: CorpusStatus,
):
    request = CorpusQualificationRequest(
        contract_id=FROZEN_CONTRACT_ID,
        expected_interval_count=expected_count,
        interval_results=(),
    )
    expected = _corpus_result(
        status,
        expected_intervals=expected_count,
        evaluated_intervals=expected_count,
        satisfied_intervals=satisfied,
        completeness_pct=percentage,
    )

    _assert_corpus_vector(request, expected)


def test_integrity_counters_remain_distinct_and_uncollapsed():
    request = CorpusQualificationRequest(
        contract_id=FROZEN_CONTRACT_ID,
        expected_interval_count=20,
        interval_results=(),
    )
    expected = _corpus_result(
        "FAIL",
        expected_intervals=20,
        evaluated_intervals=19,
        satisfied_intervals=10,
        failed_intervals=10,
        completeness_pct="50.00",
        provider_strike_set_failures=1,
        provider_wing_insufficiencies=2,
        expected_strike_omissions=3,
        expected_contract_omissions=4,
        missing_quotes=5,
        quote_inversions=6,
        invalid_asks=7,
        invalid_bids=8,
        timestamp_alignment_failures=9,
    )

    _assert_corpus_vector(request, expected)
    counters = expected.counters
    assert counters.expected_intervals == 20
    assert counters.evaluated_intervals == 19
    assert counters.satisfied_intervals == 10
    assert counters.failed_intervals == 10
    assert counters.completeness_pct == Decimal("50.00")
    assert counters.provider_strike_set_failures == 1
    assert counters.provider_wing_insufficiencies == 2
    assert counters.expected_strike_omissions == 3
    assert counters.expected_contract_omissions == 4
    assert counters.missing_quotes == 5
    assert counters.quote_inversions == 6
    assert counters.invalid_asks == 7
    assert counters.invalid_bids == 8
    assert counters.timestamp_alignment_failures == 9
