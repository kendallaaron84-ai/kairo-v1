"""Production evaluator for DYNAMIC-ENVELOPE-CONTRACT-v1.0.

The qualifier is intentionally side-effect free.  It compares an authoritative
provider-listed universe with an independently acquired universe; it performs no
discovery, slicing, provider access, persistence, or strategy selection.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final


CONTRACT_ID: Final = "DYNAMIC-ENVELOPE-CONTRACT-v1.0"
ENVELOPE_SATISFIED: Final = "ENVELOPE_SATISFIED"
DYNAMIC_ENVELOPE_DEFICIT: Final = "DYNAMIC_ENVELOPE_DEFICIT"
PASS: Final = "PASS"
MARGINAL_REVIEW: Final = "MARGINAL_REVIEW"
FAIL: Final = "FAIL"
PERCENT_QUANTUM: Final = Decimal("0.01")
PASS_THRESHOLD: Final = Decimal("95.00")
REVIEW_THRESHOLD: Final = Decimal("90.00")

PROVIDER_STRIKE_SET_UNAVAILABLE: Final = "PROVIDER_STRIKE_SET_UNAVAILABLE"
PROVIDER_WING_INSUFFICIENT: Final = "PROVIDER_WING_INSUFFICIENT"
EXPECTED_STRIKE_MISSING: Final = "EXPECTED_STRIKE_MISSING"
EXPECTED_CONTRACT_MISSING: Final = "EXPECTED_CONTRACT_MISSING"
EXPECTED_QUOTE_MISSING: Final = "EXPECTED_QUOTE_MISSING"
QUOTE_INVERSION: Final = "QUOTE_INVERSION"
INVALID_ASK: Final = "INVALID_ASK"
INVALID_BID: Final = "INVALID_BID"
TIMESTAMP_ALIGNMENT_FAILURE: Final = "TIMESTAMP_ALIGNMENT_FAILURE"

CANONICAL_REASON_ORDER: Final = (
    PROVIDER_STRIKE_SET_UNAVAILABLE,
    PROVIDER_WING_INSUFFICIENT,
    EXPECTED_STRIKE_MISSING,
    EXPECTED_CONTRACT_MISSING,
    EXPECTED_QUOTE_MISSING,
    QUOTE_INVERSION,
    INVALID_ASK,
    INVALID_BID,
    TIMESTAMP_ALIGNMENT_FAILURE,
)


@dataclass(frozen=True)
class IntervalQualification:
    status: str
    atm_strike: Decimal | None
    required_strikes: tuple[Decimal, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, eq=False)
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

    def __eq__(self, other: object) -> bool:
        """Compare structurally so protocol consumers need not import this model."""

        return all(
            getattr(other, field_name, object()) == getattr(self, field_name)
            for field_name in self.__dataclass_fields__
        )


@dataclass(frozen=True)
class CorpusQualification:
    status: str
    counters: IntegrityCounters


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _contract_identity(contract: Any) -> tuple[str, Decimal, str]:
    return (
        str(contract.contract_id),
        _decimal(contract.strike),
        str(contract.right),
    )


def _percentage(numerator: int, denominator: int) -> Decimal:
    return (Decimal(numerator) * Decimal("100") / Decimal(denominator)).quantize(
        PERCENT_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


class DynamicEnvelopeQualifier:
    """Fail-closed evaluator for interval and corpus acquisition completeness."""

    contract_id = CONTRACT_ID

    def qualify_interval(self, request: Any) -> IntervalQualification:
        self._validate_contract_id(request)
        provider_strikes = self._provider_strikes(request.expected_universe.strikes)
        reasons: set[str] = set()
        atm_strike: Decimal | None = None
        required_strikes: tuple[Decimal, ...] = ()

        if not provider_strikes:
            reasons.add(PROVIDER_STRIKE_SET_UNAVAILABLE)
        else:
            atm_strike = min(
                provider_strikes,
                key=lambda strike: (abs(strike - _decimal(request.spot)), strike),
            )
            atm_index = provider_strikes.index(atm_strike)
            lower_count = atm_index
            upper_count = len(provider_strikes) - atm_index - 1
            if lower_count < 10 or upper_count < 10:
                reasons.add(PROVIDER_WING_INSUFFICIENT)
            else:
                required_strikes = provider_strikes[atm_index - 10 : atm_index + 11]
                self._evaluate_coverage_and_quotes(
                    request=request,
                    required_strikes=required_strikes,
                    reasons=reasons,
                )

        if request.acquired_universe.timestamp != request.underlying_completed_at:
            reasons.add(TIMESTAMP_ALIGNMENT_FAILURE)

        reason_codes = tuple(
            reason for reason in CANONICAL_REASON_ORDER if reason in reasons
        )
        status = DYNAMIC_ENVELOPE_DEFICIT if reason_codes else ENVELOPE_SATISFIED
        return IntervalQualification(
            status=status,
            atm_strike=atm_strike,
            required_strikes=required_strikes,
            reason_codes=reason_codes,
        )

    def qualify_corpus(self, request: Any) -> CorpusQualification:
        self._validate_contract_id(request)
        expected_intervals = int(request.expected_interval_count)
        if expected_intervals <= 0:
            raise ValueError("expected_interval_count must be positive")

        supplied_results = tuple(request.interval_results)
        if len(supplied_results) > expected_intervals:
            raise ValueError("interval results cannot exceed the immutable denominator")
        evaluated_results = tuple(
            result for result in supplied_results if result is not None
        )
        for result in evaluated_results:
            if result.status not in (ENVELOPE_SATISFIED, DYNAMIC_ENVELOPE_DEFICIT):
                raise ValueError(f"unknown interval qualification status: {result.status}")

        satisfied_intervals = sum(
            result.status == ENVELOPE_SATISFIED for result in evaluated_results
        )
        completeness_pct = _percentage(satisfied_intervals, expected_intervals)
        if completeness_pct >= PASS_THRESHOLD:
            status = PASS
        elif completeness_pct >= REVIEW_THRESHOLD:
            status = MARGINAL_REVIEW
        else:
            status = FAIL

        counters = IntegrityCounters(
            expected_intervals=expected_intervals,
            evaluated_intervals=len(evaluated_results),
            satisfied_intervals=satisfied_intervals,
            failed_intervals=expected_intervals - satisfied_intervals,
            completeness_pct=completeness_pct,
            provider_strike_set_failures=self._count_reason(
                evaluated_results, PROVIDER_STRIKE_SET_UNAVAILABLE
            ),
            provider_wing_insufficiencies=self._count_reason(
                evaluated_results, PROVIDER_WING_INSUFFICIENT
            ),
            expected_strike_omissions=self._count_reason(
                evaluated_results, EXPECTED_STRIKE_MISSING
            ),
            expected_contract_omissions=self._count_reason(
                evaluated_results, EXPECTED_CONTRACT_MISSING
            ),
            missing_quotes=self._count_reason(
                evaluated_results, EXPECTED_QUOTE_MISSING
            ),
            quote_inversions=self._count_reason(evaluated_results, QUOTE_INVERSION),
            invalid_asks=self._count_reason(evaluated_results, INVALID_ASK),
            invalid_bids=self._count_reason(evaluated_results, INVALID_BID),
            timestamp_alignment_failures=self._count_reason(
                evaluated_results, TIMESTAMP_ALIGNMENT_FAILURE
            ),
        )
        return CorpusQualification(status=status, counters=counters)

    @staticmethod
    def _validate_contract_id(request: Any) -> None:
        if request.contract_id != CONTRACT_ID:
            raise ValueError(f"qualification request must bind to {CONTRACT_ID}")

    @staticmethod
    def _provider_strikes(values: Any) -> tuple[Decimal, ...]:
        strikes = tuple(_decimal(value) for value in values)
        if len(set(strikes)) != len(strikes):
            raise ValueError("provider strike universe must contain unique strikes")
        if any(left >= right for left, right in zip(strikes, strikes[1:])):
            raise ValueError("provider strike universe must be strictly ascending")
        return strikes

    @staticmethod
    def _evaluate_coverage_and_quotes(
        *,
        request: Any,
        required_strikes: tuple[Decimal, ...],
        reasons: set[str],
    ) -> None:
        required_set = set(required_strikes)
        expected_contracts = tuple(
            contract
            for contract in request.expected_universe.contracts
            if _decimal(contract.strike) in required_set
        )
        acquired_contracts = tuple(
            contract
            for contract in request.acquired_universe.contracts
            if _decimal(contract.strike) in required_set
        )
        acquired_strikes = {_decimal(contract.strike) for contract in acquired_contracts}
        if any(strike not in acquired_strikes for strike in required_strikes):
            reasons.add(EXPECTED_STRIKE_MISSING)

        expected_by_identity = {
            _contract_identity(contract): contract for contract in expected_contracts
        }
        acquired_identities = {
            _contract_identity(contract) for contract in acquired_contracts
        }
        if any(identity not in acquired_identities for identity in expected_by_identity):
            reasons.add(EXPECTED_CONTRACT_MISSING)

        for contract in acquired_contracts:
            identity = _contract_identity(contract)
            expected = expected_by_identity.get(identity)
            if expected is None:
                continue
            bid = None if contract.bid is None else _decimal(contract.bid)
            ask = None if contract.ask is None else _decimal(contract.ask)
            if bid is None or ask is None:
                if bool(expected.quote_expected):
                    reasons.add(EXPECTED_QUOTE_MISSING)
                continue
            if ask < bid:
                reasons.add(QUOTE_INVERSION)
            if ask <= 0:
                reasons.add(INVALID_ASK)
            if bid < 0:
                reasons.add(INVALID_BID)

    @staticmethod
    def _count_reason(results: tuple[Any, ...], reason: str) -> int:
        return sum(reason in result.reason_codes for result in results)


def build_dynamic_envelope_qualifier() -> DynamicEnvelopeQualifier:
    """Factory used by the frozen conformance suite and future composition roots."""

    return DynamicEnvelopeQualifier()
