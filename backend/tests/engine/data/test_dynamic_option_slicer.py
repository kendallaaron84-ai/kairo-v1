from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from engine.data.dynamic_option_slicer import (
    CompletedUnderlyingBar,
    DynamicOptionEvidenceSlicer,
    ProviderExpirationListing,
    ProviderListedContract,
    ProviderListingSnapshot,
    ProviderQuote,
    ProviderQuoteSnapshot,
)


BASE_TIME = datetime(2024, 1, 2, 15, 30, tzinfo=timezone.utc)
EXPIRATION = date(2024, 1, 5)
FULL_STRIKES = tuple(Decimal(value) for value in range(70, 131))
QuoteBuilder = Callable[
    [str, datetime, date, tuple[ProviderListedContract, ...]],
    ProviderQuoteSnapshot,
]


class SyntheticProvider:
    def __init__(
        self,
        discoveries: dict[datetime, ProviderListingSnapshot],
        quote_builder: QuoteBuilder | None = None,
    ) -> None:
        self.discoveries = discoveries
        self.quote_builder = quote_builder or _valid_quote_snapshot
        self.discovery_calls: list[tuple[str, datetime]] = []
        self.quote_calls: list[
            tuple[str, datetime, date, tuple[ProviderListedContract, ...]]
        ] = []

    def discover_listings(
        self,
        *,
        symbol: str,
        completed_at: datetime,
    ) -> ProviderListingSnapshot:
        self.discovery_calls.append((symbol, completed_at))
        return self.discoveries[completed_at]

    def acquire_quotes(
        self,
        *,
        symbol: str,
        completed_at: datetime,
        expiration_date: date,
        contracts: tuple[ProviderListedContract, ...],
    ) -> ProviderQuoteSnapshot:
        self.quote_calls.append((symbol, completed_at, expiration_date, contracts))
        return self.quote_builder(symbol, completed_at, expiration_date, contracts)


def _bar(minute: int, spot: str | int) -> CompletedUnderlyingBar:
    return CompletedUnderlyingBar(
        symbol="TQQQ",
        completed_at=BASE_TIME + timedelta(minutes=minute),
        close=Decimal(str(spot)),
    )


def _contracts(
    strikes: tuple[Decimal, ...],
    expiration: date = EXPIRATION,
    rights_by_strike: dict[Decimal, tuple[str, ...]] | None = None,
) -> tuple[ProviderListedContract, ...]:
    rights_by_strike = rights_by_strike or {}
    return tuple(
        ProviderListedContract(
            contract_id=(
                f"TQQQ-{expiration:%Y%m%d}-{right}-{format(strike, 'f')}"
            ),
            expiration_date=expiration,
            strike=strike,
            right=right,
        )
        for strike in strikes
        for right in rights_by_strike.get(strike, ("CALL", "PUT"))
    )


def _listing(
    timestamp: datetime,
    strikes: tuple[Decimal, ...] = FULL_STRIKES,
    *,
    rights_by_strike: dict[Decimal, tuple[str, ...]] | None = None,
    extra_expirations: tuple[ProviderExpirationListing, ...] = (),
) -> ProviderListingSnapshot:
    primary = ProviderExpirationListing(
        expiration_date=EXPIRATION,
        strikes=tuple(reversed(strikes)),
        contracts=tuple(reversed(_contracts(strikes, rights_by_strike=rights_by_strike))),
    )
    return ProviderListingSnapshot(
        symbol="TQQQ",
        timestamp=timestamp,
        discovery_succeeded=True,
        expirations=tuple(reversed((primary,) + extra_expirations)),
    )


def _valid_quote_snapshot(
    symbol: str,
    completed_at: datetime,
    expiration: date,
    contracts: tuple[ProviderListedContract, ...],
) -> ProviderQuoteSnapshot:
    quotes = tuple(
        ProviderQuote(
            contract_id=contract.contract_id,
            expiration_date=contract.expiration_date,
            strike=contract.strike,
            right=contract.right,
            bid=Decimal("1.00"),
            ask=Decimal("1.05"),
        )
        for contract in reversed(contracts)
    )
    return ProviderQuoteSnapshot(
        symbol=symbol,
        expiration_date=expiration,
        timestamp=completed_at,
        acquisition_succeeded=True,
        quotes=quotes,
    )


def _provider_for_bars(
    bars: tuple[CompletedUnderlyingBar, ...],
    *,
    strikes: tuple[Decimal, ...] = FULL_STRIKES,
    quote_builder: QuoteBuilder | None = None,
    rights_by_strike: dict[Decimal, tuple[str, ...]] | None = None,
) -> SyntheticProvider:
    return SyntheticProvider(
        {
            bar.completed_at: _listing(
                bar.completed_at,
                strikes,
                rights_by_strike=rights_by_strike,
            )
            for bar in bars
        },
        quote_builder,
    )


def _required(result) -> tuple[Decimal, ...]:
    return result.qualification.required_strikes


def test_stationary_spot_keeps_stable_provider_relative_envelope():
    bars = (_bar(0, "100.10"), _bar(1, "99.90"), _bar(2, "100.20"))
    provider = _provider_for_bars(bars)

    results = DynamicOptionEvidenceSlicer(provider).slice_bars(bars)

    expected = tuple(Decimal(value) for value in range(90, 111))
    assert tuple(_required(result) for result in results) == (expected, expected, expected)
    assert all(result.qualification.status == "ENVELOPE_SATISFIED" for result in results)
    assert len(provider.discovery_calls) == 3
    assert len(provider.quote_calls) == 3


def test_strong_rally_recenters_each_completed_minute():
    bars = (_bar(0, 100), _bar(1, 105), _bar(2, 110))
    results = DynamicOptionEvidenceSlicer(_provider_for_bars(bars)).slice_bars(bars)

    assert tuple(_required(result)[10] for result in results) == (
        Decimal("100"),
        Decimal("105"),
        Decimal("110"),
    )
    assert _required(results[0]) != _required(results[-1])


def test_strong_selloff_recenters_each_completed_minute():
    bars = (_bar(0, 100), _bar(1, 95), _bar(2, 90))
    results = DynamicOptionEvidenceSlicer(_provider_for_bars(bars)).slice_bars(bars)

    assert tuple(_required(result)[10] for result in results) == (
        Decimal("100"),
        Decimal("95"),
        Decimal("90"),
    )
    assert _required(results[0]) != _required(results[-1])


@pytest.mark.parametrize("ending_spot", [90, 110], ids=("minus-10-percent", "plus-10-percent"))
def test_large_intraday_drift_recenters_from_contemporaneous_spot(ending_spot: int):
    bars = (_bar(0, 100), _bar(1, ending_spot))
    results = DynamicOptionEvidenceSlicer(_provider_for_bars(bars)).slice_bars(bars)

    assert _required(results[0])[10] == Decimal("100")
    assert _required(results[1])[10] == Decimal(ending_spot)
    assert results[1].spot == Decimal(ending_spot)


def test_nonuniform_provider_spacing_uses_ordinal_rank_not_numeric_increment():
    strikes = tuple(
        Decimal(str(value))
        for value in (
            80,
            82,
            84,
            86,
            88,
            90,
            91,
            92,
            93,
            94,
            95,
            96,
            97,
            98,
            99,
            100,
            100.5,
            101,
            101.5,
            102,
            103,
            104,
            105,
            106,
            108,
            110,
            112,
            115,
            120,
            125,
            130,
        )
    )
    bar = _bar(0, "100.10")

    result = DynamicOptionEvidenceSlicer(
        _provider_for_bars((bar,), strikes=strikes)
    ).slice_bar(bar)[0]

    assert _required(result) == strikes[5:26]
    assert _required(result)[10] == Decimal("100")
    assert result.qualification.status == "ENVELOPE_SATISFIED"


def test_call_put_asymmetry_preserves_only_provider_listed_rights():
    bar = _bar(0, 100)
    rights = {
        Decimal("99"): ("CALL",),
        Decimal("101"): ("PUT",),
    }
    result = DynamicOptionEvidenceSlicer(
        _provider_for_bars((bar,), strikes=tuple(Decimal(v) for v in range(90, 111)),
                           rights_by_strike=rights)
    ).slice_bar(bar)[0]

    expected_rights = {
        contract.strike: contract.right
        for contract in result.expected_universe.contracts
        if contract.strike in (Decimal("99"), Decimal("101"))
    }
    assert expected_rights == {Decimal("99"): "CALL", Decimal("101"): "PUT"}
    assert len(result.expected_universe.contracts) == 40
    assert len(result.requested_contract_ids) == 40
    assert result.qualification.status == "ENVELOPE_SATISFIED"


def test_missing_quote_payload_remains_null_and_fails_qualification():
    bar = _bar(0, 100)

    def missing_quote(symbol, completed_at, expiration, contracts):
        snapshot = _valid_quote_snapshot(symbol, completed_at, expiration, contracts)
        quotes = list(snapshot.quotes)
        target = quotes[0]
        quotes[0] = ProviderQuote(
            contract_id=target.contract_id,
            expiration_date=target.expiration_date,
            strike=target.strike,
            right=target.right,
            bid=None,
            ask=None,
        )
        return ProviderQuoteSnapshot(
            symbol=symbol,
            expiration_date=expiration,
            timestamp=completed_at,
            acquisition_succeeded=True,
            quotes=tuple(quotes),
        )

    result = DynamicOptionEvidenceSlicer(
        _provider_for_bars((bar,), quote_builder=missing_quote)
    ).slice_bar(bar)[0]

    assert len(result.expected_universe.contracts) == 122
    assert any(
        contract.bid is None and contract.ask is None
        for contract in result.acquired_universe.contracts
    )
    assert result.qualification.reason_codes == ("EXPECTED_QUOTE_MISSING",)


def test_zero_bid_is_preserved_and_remains_valid_evidence():
    bar = _bar(0, 100)

    def zero_bid(symbol, completed_at, expiration, contracts):
        return ProviderQuoteSnapshot(
            symbol=symbol,
            expiration_date=expiration,
            timestamp=completed_at,
            acquisition_succeeded=True,
            quotes=tuple(
                ProviderQuote(
                    contract_id=contract.contract_id,
                    expiration_date=contract.expiration_date,
                    strike=contract.strike,
                    right=contract.right,
                    bid=Decimal("0.00"),
                    ask=Decimal("0.05"),
                )
                for contract in contracts
            ),
        )

    result = DynamicOptionEvidenceSlicer(
        _provider_for_bars((bar,), quote_builder=zero_bid)
    ).slice_bar(bar)[0]

    assert all(contract.bid == Decimal("0.00") for contract in result.acquired_universe.contracts)
    assert result.qualification.status == "ENVELOPE_SATISFIED"
    assert result.qualification.reason_codes == ()


@pytest.mark.parametrize(
    ("bid", "ask", "expected_reasons"),
    [
        (Decimal("1.10"), Decimal("1.00"), ("QUOTE_INVERSION",)),
        (Decimal("0.00"), Decimal("0.00"), ("INVALID_ASK",)),
        (Decimal("-0.01"), Decimal("1.00"), ("INVALID_BID",)),
    ],
    ids=("inversion", "zero-ask", "negative-bid"),
)
def test_corrupt_quotes_remain_raw_and_qualifier_owns_failure(
    bid: Decimal,
    ask: Decimal,
    expected_reasons: tuple[str, ...],
):
    bar = _bar(0, 100)

    def corrupt_quote(symbol, completed_at, expiration, contracts):
        snapshot = _valid_quote_snapshot(symbol, completed_at, expiration, contracts)
        quotes = list(snapshot.quotes)
        target = quotes[0]
        quotes[0] = ProviderQuote(
            contract_id=target.contract_id,
            expiration_date=target.expiration_date,
            strike=target.strike,
            right=target.right,
            bid=bid,
            ask=ask,
        )
        return ProviderQuoteSnapshot(
            symbol=symbol,
            expiration_date=expiration,
            timestamp=completed_at,
            acquisition_succeeded=True,
            quotes=tuple(quotes),
        )

    result = DynamicOptionEvidenceSlicer(
        _provider_for_bars((bar,), quote_builder=corrupt_quote)
    ).slice_bar(bar)[0]

    assert any(
        contract.bid == bid and contract.ask == ask
        for contract in result.acquired_universe.contracts
    )
    assert result.qualification.reason_codes == expected_reasons


def test_provider_listing_failure_never_derives_expected_from_quotes():
    bar = _bar(0, 100)
    provider = SyntheticProvider(
        {
            bar.completed_at: ProviderListingSnapshot(
                symbol="TQQQ",
                timestamp=bar.completed_at,
                discovery_succeeded=False,
                expirations=(),
            )
        }
    )

    result = DynamicOptionEvidenceSlicer(provider).slice_bar(bar)[0]

    assert result.expected_universe.strikes == ()
    assert result.expected_universe.contracts == ()
    assert result.acquired_universe.contracts == ()
    assert provider.quote_calls == []
    assert result.qualification.reason_codes == ("PROVIDER_STRIKE_SET_UNAVAILABLE",)


def test_provider_wing_insufficiency_does_not_trigger_reduced_quote_request():
    bar = _bar(0, 100)
    strikes = tuple(Decimal(value) for value in range(91, 111))
    provider = _provider_for_bars((bar,), strikes=strikes)

    result = DynamicOptionEvidenceSlicer(provider).slice_bar(bar)[0]

    assert provider.quote_calls == []
    assert result.requested_contract_ids == ()
    assert result.qualification.status == "DYNAMIC_ENVELOPE_DEFICIT"
    assert result.qualification.reason_codes == ("PROVIDER_WING_INSUFFICIENT",)


def test_quote_acquisition_failure_preserves_expected_provider_reality():
    bar = _bar(0, 100)

    def failed_quotes(symbol, completed_at, expiration, _contracts):
        return ProviderQuoteSnapshot(
            symbol=symbol,
            expiration_date=expiration,
            timestamp=completed_at,
            acquisition_succeeded=False,
            quotes=(),
        )

    result = DynamicOptionEvidenceSlicer(
        _provider_for_bars((bar,), quote_builder=failed_quotes)
    ).slice_bar(bar)[0]

    assert result.quote_acquisition_succeeded is False
    assert len(result.expected_universe.contracts) == 122
    assert result.acquired_universe.contracts == ()
    assert result.qualification.reason_codes == (
        "EXPECTED_STRIKE_MISSING",
        "EXPECTED_CONTRACT_MISSING",
    )


def test_quote_timestamp_drift_is_preserved_for_qualifier_rejection():
    bar = _bar(0, 100)

    def drifted_quotes(symbol, completed_at, expiration, contracts):
        snapshot = _valid_quote_snapshot(symbol, completed_at, expiration, contracts)
        return ProviderQuoteSnapshot(
            symbol=snapshot.symbol,
            expiration_date=snapshot.expiration_date,
            timestamp=completed_at + timedelta(seconds=1),
            acquisition_succeeded=True,
            quotes=snapshot.quotes,
        )

    result = DynamicOptionEvidenceSlicer(
        _provider_for_bars((bar,), quote_builder=drifted_quotes)
    ).slice_bar(bar)[0]

    assert result.acquired_universe.timestamp == bar.completed_at + timedelta(seconds=1)
    assert result.qualification.reason_codes == ("TIMESTAMP_ALIGNMENT_FAILURE",)


def test_anti_circularity_keeps_42_expected_when_41_clean_quotes_return():
    bar = _bar(0, 100)
    strikes = tuple(Decimal(value) for value in range(90, 111))

    def one_missing(symbol, completed_at, expiration, contracts):
        snapshot = _valid_quote_snapshot(symbol, completed_at, expiration, contracts)
        return ProviderQuoteSnapshot(
            symbol=symbol,
            expiration_date=expiration,
            timestamp=completed_at,
            acquisition_succeeded=True,
            quotes=snapshot.quotes[:-1],
        )

    result = DynamicOptionEvidenceSlicer(
        _provider_for_bars((bar,), strikes=strikes, quote_builder=one_missing)
    ).slice_bar(bar)[0]

    assert len(result.expected_universe.contracts) == 42
    assert len(result.acquired_universe.contracts) == 41
    assert all(
        contract.bid == Decimal("1.00") and contract.ask == Decimal("1.05")
        for contract in result.acquired_universe.contracts
    )
    assert result.qualification.status == "DYNAMIC_ENVELOPE_DEFICIT"
    assert result.qualification.reason_codes == ("EXPECTED_CONTRACT_MISSING",)


def test_out_of_scope_expirations_are_not_requested():
    bar = _bar(0, 100)
    far_expiration = date(2024, 1, 12)
    far = ProviderExpirationListing(
        expiration_date=far_expiration,
        strikes=FULL_STRIKES,
        contracts=_contracts(FULL_STRIKES, expiration=far_expiration),
    )
    provider = SyntheticProvider(
        {bar.completed_at: _listing(bar.completed_at, extra_expirations=(far,))}
    )

    results = DynamicOptionEvidenceSlicer(provider).slice_bar(bar)

    assert tuple(result.expiration_date for result in results) == (EXPIRATION,)
    assert tuple(call[2] for call in provider.quote_calls) == (EXPIRATION,)


def test_identical_inputs_produce_structurally_identical_outputs():
    bar = _bar(0, 100)
    provider = _provider_for_bars((bar,))
    slicer = DynamicOptionEvidenceSlicer(provider)

    first = slicer.slice_bar(bar)
    second = slicer.slice_bar(bar)

    assert first == second
