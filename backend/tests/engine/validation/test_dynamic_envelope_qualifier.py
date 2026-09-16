"""Executable black-box contract for DYNAMIC-ENVELOPE-CONTRACT-v1.0.

The suite contains synthetic evidence and expected externally observable behavior,
but no qualification or slicing algorithm.  Supply an implementation factory as
``module.name:factory`` or ``path/to/module.py:factory`` through the environment
variable ``KAIRO_DYNAMIC_ENVELOPE_QUALIFIER_FACTORY``.  The factory must return an
object satisfying :class:`DynamicEnvelopeQualifier`.
"""

import importlib
import importlib.util
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Literal, Protocol, cast, runtime_checkable

import pytest


FROZEN_CONTRACT_ID = "DYNAMIC-ENVELOPE-CONTRACT-v1.0"
IMPLEMENTATION_FACTORY_ENV = "KAIRO_DYNAMIC_ENVELOPE_QUALIFIER_FACTORY"
COMPLETED_AT = datetime(2024, 1, 2, 15, 30, tzinfo=timezone.utc)
ReasonCode = Literal[
    "PROVIDER_STRIKE_SET_UNAVAILABLE",
    "PROVIDER_WING_INSUFFICIENT",
    "EXPECTED_STRIKE_MISSING",
    "EXPECTED_CONTRACT_MISSING",
    "EXPECTED_QUOTE_MISSING",
    "QUOTE_INVERSION",
    "INVALID_ASK",
    "INVALID_BID",
    "TIMESTAMP_ALIGNMENT_FAILURE",
]
CANONICAL_REASON_ORDER: tuple[ReasonCode, ...] = (
    "PROVIDER_STRIKE_SET_UNAVAILABLE",
    "PROVIDER_WING_INSUFFICIENT",
    "EXPECTED_STRIKE_MISSING",
    "EXPECTED_CONTRACT_MISSING",
    "EXPECTED_QUOTE_MISSING",
    "QUOTE_INVERSION",
    "INVALID_ASK",
    "INVALID_BID",
    "TIMESTAMP_ALIGNMENT_FAILURE",
)
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


@runtime_checkable
class DynamicEnvelopeQualifier(Protocol):
    """Black-box interface an injected qualifier implementation must satisfy."""

    def qualify_interval(
        self, request: IntervalQualificationRequest
    ) -> IntervalQualificationResult: ...

    def qualify_corpus(self, request: CorpusQualificationRequest) -> CorpusQualificationResult: ...


def _load_factory_module(module_reference: str) -> ModuleType:
    path = Path(module_reference)
    if path.is_file():
        spec = importlib.util.spec_from_file_location("dynamic_envelope_injected", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load qualifier module from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return importlib.import_module(module_reference)


@pytest.fixture(scope="session")
def qualifier() -> DynamicEnvelopeQualifier:
    """Load, instantiate, and structurally validate the external implementation."""

    reference = os.getenv(IMPLEMENTATION_FACTORY_ENV)
    if not reference:
        pytest.skip(f"set {IMPLEMENTATION_FACTORY_ENV}=module:factory to run contract vectors")
    module_reference, separator, factory_name = reference.rpartition(":")
    if not separator or not module_reference or not factory_name:
        pytest.fail(
            f"{IMPLEMENTATION_FACTORY_ENV} must use module:factory or file.py:factory syntax"
        )
    module = _load_factory_module(module_reference)
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        pytest.fail(f"injected qualifier factory is not callable: {reference}")
    implementation = factory()
    if not isinstance(implementation, DynamicEnvelopeQualifier):
        pytest.fail("injected object does not satisfy DynamicEnvelopeQualifier")
    return cast(DynamicEnvelopeQualifier, implementation)


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


def _assert_interval_vector(
    qualifier: DynamicEnvelopeQualifier,
    request: IntervalQualificationRequest,
    expected: IntervalQualificationResult,
) -> None:
    result = qualifier.qualify_interval(request)
    assert result.status == expected.status
    assert result.atm_strike == expected.atm_strike
    assert result.required_strikes == expected.required_strikes
    assert result.reason_codes == expected.reason_codes


def _assert_corpus_vector(
    qualifier: DynamicEnvelopeQualifier,
    request: CorpusQualificationRequest,
    expected_status: CorpusStatus,
    expected_counters: IntegrityCounters,
) -> None:
    result = qualifier.qualify_corpus(request)
    assert result.status == expected_status
    assert result.counters == expected_counters


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
SATISFIED = _result(
    "ENVELOPE_SATISFIED",
    atm=Decimal("100"),
    required=UNIFORM_STRIKES,
)


def _failed(*reasons: ReasonCode) -> IntervalQualificationResult:
    ordered = tuple(reason for reason in CANONICAL_REASON_ORDER if reason in reasons)
    return _result(
        "DYNAMIC_ENVELOPE_DEFICIT",
        atm=None,
        reasons=ordered,
    )


def _expected_counters(
    *,
    expected_intervals: int,
    evaluated_intervals: int,
    satisfied_intervals: int,
    completeness_pct: str,
    provider_strike_set_failures: int = 0,
    provider_wing_insufficiencies: int = 0,
    expected_strike_omissions: int = 0,
    expected_contract_omissions: int = 0,
    missing_quotes: int = 0,
    quote_inversions: int = 0,
    invalid_asks: int = 0,
    invalid_bids: int = 0,
    timestamp_alignment_failures: int = 0,
) -> IntegrityCounters:
    return IntegrityCounters(
        expected_intervals=expected_intervals,
        evaluated_intervals=evaluated_intervals,
        satisfied_intervals=satisfied_intervals,
        failed_intervals=expected_intervals - satisfied_intervals,
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
    )


def test_inputs_keep_expected_and_acquired_universes_structurally_independent():
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


def test_missing_expected_strike_preserves_all_applicable_attribution(
    qualifier: DynamicEnvelopeQualifier,
):
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
        reasons=("EXPECTED_STRIKE_MISSING", "EXPECTED_CONTRACT_MISSING"),
    )

    _assert_interval_vector(qualifier, request, expected)


def test_atm_tie_resolves_to_lower_listed_strike_and_uses_ordinal_window(
    qualifier: DynamicEnvelopeQualifier,
):
    provider_strikes = tuple(Decimal(value) for value in range(90, 112))
    required = tuple(Decimal(value) for value in range(90, 111))
    request = _request(provider_strikes, spot=Decimal("100.50"))
    expected = _result("ENVELOPE_SATISFIED", atm=Decimal("100"), required=required)

    _assert_interval_vector(qualifier, request, expected)


def test_non_uniform_provider_steps_pass_when_all_21_ordinal_strikes_are_present(
    qualifier: DynamicEnvelopeQualifier,
):
    request = _request(NON_UNIFORM_STRIKES, spot=Decimal("100.10"))
    expected = _result(
        "ENVELOPE_SATISFIED",
        atm=Decimal("100"),
        required=NON_UNIFORM_STRIKES,
    )

    _assert_interval_vector(qualifier, request, expected)


@pytest.mark.parametrize(
    ("provider_strikes", "spot"),
    [
        (tuple(Decimal(value) for value in range(91, 111)), Decimal("100")),
        (tuple(Decimal(value) for value in range(90, 110)), Decimal("100")),
    ],
    ids=("nine-below", "nine-above"),
)
def test_provider_wing_with_fewer_than_ten_listed_strikes_fails(
    qualifier: DynamicEnvelopeQualifier,
    provider_strikes: tuple[Decimal, ...],
    spot: Decimal,
):
    request = _request(provider_strikes, spot=spot)
    expected = _result(
        "DYNAMIC_ENVELOPE_DEFICIT",
        atm=Decimal("100"),
        reasons=("PROVIDER_WING_INSUFFICIENT",),
    )

    _assert_interval_vector(qualifier, request, expected)


def test_unavailable_provider_strike_set_fails_closed(
    qualifier: DynamicEnvelopeQualifier,
):
    request = _request((), spot=Decimal("100"))
    expected = _result(
        "DYNAMIC_ENVELOPE_DEFICIT",
        atm=None,
        reasons=("PROVIDER_STRIKE_SET_UNAVAILABLE",),
    )

    _assert_interval_vector(qualifier, request, expected)


@pytest.mark.parametrize("missing_right", ["CALL", "PUT"])
def test_each_provider_listed_right_is_independently_required(
    qualifier: DynamicEnvelopeQualifier,
    missing_right: OptionRight,
):
    listed = _listed_contracts(UNIFORM_STRIKES)
    acquired = tuple(
        contract
        for contract in _acquired_contracts(listed)
        if not (contract.strike == Decimal("101") and contract.right == missing_right)
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

    _assert_interval_vector(qualifier, request, expected)


@pytest.mark.parametrize("sole_right", ["CALL", "PUT"])
def test_asymmetric_provider_listing_does_not_synthesize_opposite_right(
    qualifier: DynamicEnvelopeQualifier,
    sole_right: OptionRight,
):
    listed = tuple(
        contract
        for contract in _listed_contracts(UNIFORM_STRIKES)
        if contract.strike != Decimal("101") or contract.right == sole_right
    )
    request = _request(
        UNIFORM_STRIKES,
        spot=Decimal("100"),
        expected_contracts=listed,
        acquired_contracts=_acquired_contracts(listed),
    )
    expected = _result(
        "ENVELOPE_SATISFIED",
        atm=Decimal("100"),
        required=UNIFORM_STRIKES,
    )

    _assert_interval_vector(qualifier, request, expected)


@pytest.mark.parametrize(
    ("bid", "ask", "reasons"),
    [
        (Decimal("1.10"), Decimal("1.00"), ("QUOTE_INVERSION",)),
        (Decimal("0.00"), Decimal("0.00"), ("INVALID_ASK",)),
        (
            Decimal("0.00"),
            Decimal("-0.01"),
            ("QUOTE_INVERSION", "INVALID_ASK"),
        ),
        (Decimal("-0.01"), Decimal("1.00"), ("INVALID_BID",)),
    ],
    ids=("inverted", "zero-ask", "negative-ask", "negative-bid"),
)
def test_corrupt_required_quote_fails_without_silent_cleansing(
    qualifier: DynamicEnvelopeQualifier,
    bid: Decimal,
    ask: Decimal,
    reasons: tuple[ReasonCode, ...],
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
        reasons=reasons,
    )

    assert len(request.acquired_universe.contracts) == 42
    assert request.acquired_universe.contracts[20].bid == bid
    assert request.acquired_universe.contracts[20].ask == ask
    _assert_interval_vector(qualifier, request, expected)


def test_missing_required_quote_payload_fails_closed(
    qualifier: DynamicEnvelopeQualifier,
):
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

    _assert_interval_vector(qualifier, request, expected)


def test_zero_bid_is_preserved_as_valid_evidence(qualifier: DynamicEnvelopeQualifier):
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

    assert all(
        contract.bid == Decimal("0.00") for contract in request.acquired_universe.contracts
    )
    _assert_interval_vector(qualifier, request, expected)


@pytest.mark.parametrize(
    "drift",
    [timedelta(microseconds=1), timedelta(seconds=30), timedelta(minutes=1)],
    ids=("sub-second-drift", "same-minute-drift", "nearest-neighbor-minute"),
)
def test_any_snapshot_timestamp_drift_fails_without_nearest_neighbor_substitution(
    qualifier: DynamicEnvelopeQualifier,
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

    _assert_interval_vector(qualifier, request, expected)


def test_multiple_independent_failures_are_all_preserved_in_canonical_order(
    qualifier: DynamicEnvelopeQualifier,
):
    listed = _listed_contracts(UNIFORM_STRIKES)
    acquired = list(_acquired_contracts(listed))
    corrupt = acquired[20]
    acquired[20] = AcquiredContract(
        contract_id=corrupt.contract_id,
        strike=corrupt.strike,
        right=corrupt.right,
        bid=Decimal("1.10"),
        ask=Decimal("1.00"),
    )
    request = _request(
        UNIFORM_STRIKES,
        spot=Decimal("100"),
        expected_contracts=listed,
        acquired_contracts=tuple(acquired),
        snapshot_timestamp=COMPLETED_AT + timedelta(seconds=1),
    )
    expected = _result(
        "DYNAMIC_ENVELOPE_DEFICIT",
        atm=Decimal("100"),
        required=UNIFORM_STRIKES,
        reasons=("QUOTE_INVERSION", "TIMESTAMP_ALIGNMENT_FAILURE"),
    )

    _assert_interval_vector(qualifier, request, expected)


def test_missing_strike_does_not_hide_invalid_ask_on_surviving_contract(
    qualifier: DynamicEnvelopeQualifier,
):
    listed = _listed_contracts(UNIFORM_STRIKES)
    acquired = [
        contract
        for contract in _acquired_contracts(listed)
        if contract.strike != Decimal("96")
    ]
    corrupt = acquired[20]
    acquired[20] = AcquiredContract(
        contract_id=corrupt.contract_id,
        strike=corrupt.strike,
        right=corrupt.right,
        bid=Decimal("0.00"),
        ask=Decimal("0.00"),
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
        reasons=(
            "EXPECTED_STRIKE_MISSING",
            "EXPECTED_CONTRACT_MISSING",
            "INVALID_ASK",
        ),
    )

    _assert_interval_vector(qualifier, request, expected)


def test_empty_acquired_payload_is_acquisition_absence_not_provider_unavailability(
    qualifier: DynamicEnvelopeQualifier,
):
    request = _request(
        UNIFORM_STRIKES,
        spot=Decimal("100"),
        acquired_contracts=(),
    )
    expected = _result(
        "DYNAMIC_ENVELOPE_DEFICIT",
        atm=Decimal("100"),
        required=UNIFORM_STRIKES,
        reasons=("EXPECTED_STRIKE_MISSING", "EXPECTED_CONTRACT_MISSING"),
    )

    assert request.expected_universe.strikes == UNIFORM_STRIKES
    assert request.acquired_universe.contracts == ()
    assert "PROVIDER_STRIKE_SET_UNAVAILABLE" not in expected.reason_codes
    _assert_interval_vector(qualifier, request, expected)


def test_acquired_self_consistency_cannot_replace_provider_expected_universe(
    qualifier: DynamicEnvelopeQualifier,
):
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

    assert all(contract.bid is not None for contract in request.acquired_universe.contracts)
    assert all(contract.ask is not None for contract in request.acquired_universe.contracts)
    assert len(request.acquired_universe.contracts) == 41
    _assert_interval_vector(qualifier, request, expected)


def test_provider_defined_numerical_gaps_are_not_acquisition_defects(
    qualifier: DynamicEnvelopeQualifier,
):
    provider_strikes = _strikes(
        40,
        41,
        42,
        43,
        44,
        45,
        46,
        47,
        48,
        49,
        50,
        51,
        52,
        54,
        56,
        58,
        60,
        62,
        64,
        66,
        68,
    )
    request = _request(provider_strikes, spot=Decimal("50"))
    expected = _result(
        "ENVELOPE_SATISFIED",
        atm=Decimal("50"),
        required=provider_strikes,
    )

    assert Decimal("53") not in request.expected_universe.strikes
    assert Decimal("55") not in request.expected_universe.strikes
    _assert_interval_vector(qualifier, request, expected)


def test_missing_regular_session_interval_remains_in_390_interval_denominator(
    qualifier: DynamicEnvelopeQualifier,
):
    request = CorpusQualificationRequest(
        contract_id=FROZEN_CONTRACT_ID,
        expected_interval_count=390,
        interval_results=(SATISFIED,) * 389 + (None,),
    )
    counters = _expected_counters(
        expected_intervals=390,
        evaluated_intervals=389,
        satisfied_intervals=389,
        completeness_pct="99.74",
    )

    _assert_corpus_vector(qualifier, request, "PASS", counters)


def test_failed_retrieval_and_corrupt_interval_cannot_reduce_denominator(
    qualifier: DynamicEnvelopeQualifier,
):
    request = CorpusQualificationRequest(
        contract_id=FROZEN_CONTRACT_ID,
        expected_interval_count=390,
        interval_results=(
            None,
            _failed("PROVIDER_STRIKE_SET_UNAVAILABLE"),
            _failed("QUOTE_INVERSION"),
        ),
    )
    counters = _expected_counters(
        expected_intervals=390,
        evaluated_intervals=2,
        satisfied_intervals=0,
        completeness_pct="0.00",
        provider_strike_set_failures=1,
        quote_inversions=1,
    )

    _assert_corpus_vector(qualifier, request, "FAIL", counters)


@pytest.mark.parametrize(
    ("satisfied", "failed", "percentage", "status"),
    [
        (9500, 500, "95.00", "PASS"),
        (9499, 501, "94.99", "MARGINAL_REVIEW"),
        (9000, 1000, "90.00", "MARGINAL_REVIEW"),
        (8999, 1001, "89.99", "FAIL"),
    ],
    ids=("pass-95.00", "marginal-94.99", "marginal-90.00", "fail-89.99"),
)
def test_exact_corpus_boundaries_are_derived_from_interval_evidence(
    qualifier: DynamicEnvelopeQualifier,
    satisfied: int,
    failed: int,
    percentage: str,
    status: CorpusStatus,
):
    intervals = (SATISFIED,) * satisfied + (_failed("INVALID_ASK"),) * failed
    request = CorpusQualificationRequest(
        contract_id=FROZEN_CONTRACT_ID,
        expected_interval_count=10000,
        interval_results=intervals,
    )
    counters = _expected_counters(
        expected_intervals=10000,
        evaluated_intervals=10000,
        satisfied_intervals=satisfied,
        completeness_pct=percentage,
        invalid_asks=failed,
    )

    assert len(request.interval_results) == 10000
    _assert_corpus_vector(qualifier, request, status, counters)


def test_integrity_counters_are_aggregated_from_distinct_failure_evidence(
    qualifier: DynamicEnvelopeQualifier,
):
    failure_inventory: tuple[tuple[ReasonCode, int], ...] = (
        ("PROVIDER_STRIKE_SET_UNAVAILABLE", 1),
        ("PROVIDER_WING_INSUFFICIENT", 2),
        ("EXPECTED_STRIKE_MISSING", 3),
        ("EXPECTED_CONTRACT_MISSING", 4),
        ("EXPECTED_QUOTE_MISSING", 5),
        ("QUOTE_INVERSION", 6),
        ("INVALID_ASK", 7),
        ("INVALID_BID", 8),
        ("TIMESTAMP_ALIGNMENT_FAILURE", 9),
    )
    failures = tuple(
        result
        for reason, count in failure_inventory
        for result in (_failed(reason),) * count
    )
    intervals = (SATISFIED,) * 55 + failures
    request = CorpusQualificationRequest(
        contract_id=FROZEN_CONTRACT_ID,
        expected_interval_count=100,
        interval_results=intervals,
    )
    counters = _expected_counters(
        expected_intervals=100,
        evaluated_intervals=100,
        satisfied_intervals=55,
        completeness_pct="55.00",
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

    assert len(failures) == 45
    assert len(request.interval_results) == 100
    _assert_corpus_vector(qualifier, request, "FAIL", counters)
