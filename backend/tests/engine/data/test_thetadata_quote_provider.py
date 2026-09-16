from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import math
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from thetadata.errors import AuthenticationError, NoDataFoundError

from engine.data.dynamic_option_slicer import ProviderListedContract
from engine.data.thetadata_quote_provider import (
    ThetaDataHistoricalQuoteProvider,
    ThetaProviderQuote,
    ThetaQuoteAuthenticationError,
    ThetaQuoteIdentityError,
    ThetaQuoteTransportError,
)


NEW_YORK = ZoneInfo("America/New_York")
COMPLETED_AT = datetime(2024, 1, 2, 9, 31, tzinfo=NEW_YORK)
EXPIRATION = date(2024, 1, 5)


class RowFrame:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.columns = list(rows[0]) if rows else list(_row().keys())

    def to_dicts(self) -> list[dict[str, Any]]:
        return list(self._rows)


class FakeThetaClient:
    def __init__(
        self,
        responses: dict[str, list[dict[str, Any]] | Exception],
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def option_history_quote(self, **kwargs: Any) -> RowFrame:
        self.calls.append(("option_history_quote", kwargs))
        response = self.responses[kwargs["strike"]]
        if isinstance(response, Exception):
            raise response
        return RowFrame(response)

    def option_list_contracts(self, **kwargs: Any) -> None:
        raise AssertionError("contract discovery is prohibited")

    def option_list_strikes(self, **kwargs: Any) -> None:
        raise AssertionError("strike discovery is prohibited")

    def option_list_expirations(self, **kwargs: Any) -> None:
        raise AssertionError("expiration discovery is prohibited")


def _contract(strike: str = "50", right: str = "CALL") -> ProviderListedContract:
    return ProviderListedContract(
        contract_id=f"TQQQ-20240105-{strike}-{right}",
        expiration_date=EXPIRATION,
        strike=Decimal(strike),
        right=right,
    )


def _row(
    *,
    strike: float = 50.0,
    right: str = "CALL",
    timestamp: datetime = COMPLETED_AT,
    bid: Any = 0.72,
    ask: Any = 0.74,
) -> dict[str, Any]:
    return {
        "symbol": "TQQQ",
        "expiration": "2024-01-05",
        "strike": strike,
        "right": right,
        "timestamp": timestamp,
        "bid_size": 1,
        "bid_exchange": 10,
        "bid": bid,
        "bid_condition": 0,
        "ask_size": 229,
        "ask_exchange": 11,
        "ask": ask,
        "ask_condition": 0,
    }


def _acquire(client: FakeThetaClient, contracts=None):
    contracts = contracts or (_contract(),)
    return ThetaDataHistoricalQuoteProvider(client).acquire_quotes(
        symbol="TQQQ",
        completed_at=COMPLETED_AT,
        expiration_date=EXPIRATION,
        contracts=contracts,
    )


def test_finite_quote_maps_all_raw_fields_without_cleansing() -> None:
    client = FakeThetaClient({"50": [_row()]})

    snapshot = _acquire(client)

    assert snapshot.acquisition_succeeded is True
    assert snapshot.timestamp is COMPLETED_AT
    assert len(snapshot.quotes) == 1
    quote = snapshot.quotes[0]
    assert isinstance(quote, ThetaProviderQuote)
    assert quote.bid == Decimal("0.72")
    assert quote.ask == Decimal("0.74")
    assert quote.raw_bid == 0.72
    assert quote.raw_ask == 0.74
    assert quote.bid_size == 1
    assert quote.ask_size == 229
    assert quote.bid_exchange == 10
    assert quote.ask_exchange == 11
    assert quote.bid_condition == 0
    assert quote.ask_condition == 0


def test_nan_quote_remains_represented_with_raw_missing_state() -> None:
    client = FakeThetaClient({"50": [_row(bid=float("nan"), ask=float("nan"))]})

    quote = _acquire(client).quotes[0]

    assert quote.bid is None
    assert quote.ask is None
    assert math.isnan(quote.raw_bid)
    assert math.isnan(quote.raw_ask)


@pytest.mark.parametrize(
    "bid,ask",
    [
        (0.0, 0.01),
        (1.2, 1.1),
        (-0.01, 0.02),
    ],
)
def test_zero_inverted_and_negative_quotes_are_preserved(bid: float, ask: float) -> None:
    client = FakeThetaClient({"50": [_row(bid=bid, ask=ask)]})

    quote = _acquire(client).quotes[0]

    assert quote.bid == Decimal(str(bid))
    assert quote.ask == Decimal(str(ask))
    assert quote.raw_bid == bid
    assert quote.raw_ask == ask


@pytest.mark.parametrize(
    "response",
    [[], NoDataFoundError("no data")],
)
def test_empty_or_no_data_response_is_observable_missing_acquisition(
    response: list[dict[str, Any]] | Exception,
) -> None:
    client = FakeThetaClient({"50": response})

    snapshot = _acquire(client)

    assert snapshot.acquisition_succeeded is False
    assert snapshot.quotes == ()


@pytest.mark.parametrize(
    "contradiction",
    [
        {"symbol": "SQQQ"},
        {"expiration": "2024-01-12"},
        {"strike": 51.0},
        {"right": "PUT"},
    ],
)
def test_returned_contract_identity_mismatch_fails_closed(
    contradiction: dict[str, Any],
) -> None:
    row = {**_row(), **contradiction}
    client = FakeThetaClient({"50": [row]})

    with pytest.raises(ThetaQuoteIdentityError):
        _acquire(client)


def test_missing_contract_does_not_remove_successful_sibling() -> None:
    client = FakeThetaClient(
        {
            "50": [_row()],
            "55": NoDataFoundError("no data"),
        }
    )

    snapshot = _acquire(client, (_contract("55"), _contract("50")))

    assert snapshot.acquisition_succeeded is False
    assert [quote.contract_id for quote in snapshot.quotes] == [
        "TQQQ-20240105-50-CALL"
    ]
    assert [call[1]["strike"] for call in client.calls] == ["50", "55"]


def test_exact_timestamp_and_one_minute_request_boundary_are_retained() -> None:
    client = FakeThetaClient({"50": [_row()]})

    quote = _acquire(client).quotes[0]

    assert quote.provider_timestamp is COMPLETED_AT
    assert client.calls == [
        (
            "option_history_quote",
            {
                "symbol": "TQQQ",
                "expiration": EXPIRATION,
                "interval": "1m",
                "date": date(2024, 1, 2),
                "strike": "50",
                "right": "call",
                "start_time": datetime.strptime("09:31:00", "%H:%M:%S").time(),
                "end_time": datetime.strptime("09:31:59.999000", "%H:%M:%S.%f").time(),
            },
        )
    ]


def test_contract_requests_are_deterministically_ordered() -> None:
    client = FakeThetaClient(
        {
            "45": [_row(strike=45.0)],
            "50": [_row()],
            "55": [_row(strike=55.0)],
        }
    )

    _acquire(client, (_contract("55"), _contract("45"), _contract("50")))

    assert [call[1]["strike"] for call in client.calls] == ["45", "50", "55"]


def test_authentication_failure_is_explicit_and_does_not_fallback() -> None:
    def failing_factory(**kwargs: Any):
        raise AuthenticationError("")

    with pytest.raises(ThetaQuoteAuthenticationError):
        ThetaDataHistoricalQuoteProvider.from_api_key(
            api_key="synthetic-key",
            client_factory=failing_factory,
        )


def test_provider_failure_is_explicit() -> None:
    client = FakeThetaClient({"50": OSError("network down")})

    with pytest.raises(ThetaQuoteTransportError):
        _acquire(client)


def test_provider_has_no_contract_discovery_behavior() -> None:
    client = FakeThetaClient({"50": [_row()]})

    _acquire(client)

    assert {endpoint for endpoint, _ in client.calls} == {"option_history_quote"}
