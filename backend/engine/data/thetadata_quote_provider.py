"""Expected-driven historical option quote acquisition from ThetaData.

This module has no listing or discovery operations. Every request is constructed
from a contract supplied by the independent expected-universe plane.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import math
import os
from pathlib import Path
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

import grpc
from dotenv import load_dotenv
from thetadata import ThetaClient
from thetadata.errors import AuthenticationError, NoDataFoundError

from engine.data.dynamic_option_slicer import (
    ProviderListedContract,
    ProviderQuote,
    ProviderQuoteSnapshot,
)


NEW_YORK = ZoneInfo("America/New_York")
REQUIRED_FIELDS = frozenset(
    {
        "symbol",
        "expiration",
        "strike",
        "right",
        "timestamp",
        "bid",
        "ask",
        "bid_size",
        "ask_size",
        "bid_exchange",
        "ask_exchange",
        "bid_condition",
        "ask_condition",
    }
)


class ThetaQuoteClient(Protocol):
    def option_history_quote(self, **kwargs: Any) -> Any: ...


class ThetaQuoteProviderError(RuntimeError):
    """Base class for fail-closed quote-provider errors."""


class ThetaQuoteAuthenticationError(ThetaQuoteProviderError):
    """ThetaData authentication failed."""


class ThetaQuoteEntitlementError(ThetaQuoteProviderError):
    """ThetaData rejected historical option quote access."""


class ThetaQuoteTransportError(ThetaQuoteProviderError):
    """ThetaData could not complete a historical quote request."""


class ThetaQuoteSchemaError(ThetaQuoteProviderError):
    """ThetaData returned an unusable or unexpectedly broad schema."""


class ThetaQuoteIdentityError(ThetaQuoteProviderError):
    """ThetaData returned evidence for a different contract."""


class ThetaQuoteTimestampError(ThetaQuoteProviderError):
    """ThetaData returned evidence outside the requested interval."""


@dataclass(frozen=True, kw_only=True)
class ThetaProviderQuote(ProviderQuote):
    """Qualifier-facing quote plus the unmodified ThetaData row fields."""

    provider_timestamp: datetime
    raw_bid: Any
    raw_ask: Any
    bid_size: Any
    ask_size: Any
    bid_exchange: Any
    ask_exchange: Any
    bid_condition: Any
    ask_condition: Any


@dataclass(frozen=True)
class ThetaContractQuoteWindow:
    """Raw mapped rows for one explicitly supplied contract and time window."""

    symbol: str
    expiration_date: date
    start_at: datetime
    end_at: datetime
    contract: ProviderListedContract
    acquisition_succeeded: bool
    quotes: tuple[ThetaProviderQuote, ...]


class ThetaDataHistoricalQuoteProvider:
    """Acquire one-minute ThetaData evidence for expected contracts only."""

    def __init__(self, client: ThetaQuoteClient) -> None:
        self._client = client

    @classmethod
    def from_api_key(
        cls,
        *,
        api_key: str | None = None,
        dotenv_path: str | Path | None = None,
        client_factory: Callable[..., ThetaQuoteClient] = ThetaClient,
    ) -> ThetaDataHistoricalQuoteProvider:
        if api_key is None and dotenv_path is not None:
            load_dotenv(dotenv_path=dotenv_path)
        resolved_key = api_key or os.getenv("THETADATA_API_KEY")
        if not resolved_key:
            raise ThetaQuoteAuthenticationError("THETADATA_API_KEY is required")
        try:
            client = client_factory(
                api_key=resolved_key,
                dotenv_path=dotenv_path,
                dataframe_type="polars",
            )
        except AuthenticationError as exc:
            raise ThetaQuoteAuthenticationError("ThetaData authentication failed") from exc
        except Exception as exc:
            raise ThetaQuoteTransportError("ThetaData client initialization failed") from exc
        return cls(client)

    def acquire_quotes(
        self,
        *,
        symbol: str,
        completed_at: datetime,
        expiration_date: date,
        contracts: tuple[ProviderListedContract, ...],
    ) -> ProviderQuoteSnapshot:
        requested_symbol = _validate_request_context(
            symbol=symbol,
            completed_at=completed_at,
            expiration_date=expiration_date,
            contracts=contracts,
        )
        ordered_contracts = tuple(sorted(contracts, key=_contract_order_key))
        quotes: list[ThetaProviderQuote] = []
        acquisition_succeeded = True
        for contract in ordered_contracts:
            window = self.fetch_contract_window(
                symbol=requested_symbol,
                expiration_date=expiration_date,
                contract=contract,
                start_at=completed_at,
                end_at=completed_at,
            )
            if not window.acquisition_succeeded:
                acquisition_succeeded = False
                continue
            if len(window.quotes) != 1:
                raise ThetaQuoteSchemaError(
                    "one-minute request must return exactly one mapped interval"
                )
            quotes.append(window.quotes[0])
        return ProviderQuoteSnapshot(
            symbol=requested_symbol,
            expiration_date=expiration_date,
            timestamp=completed_at,
            acquisition_succeeded=acquisition_succeeded,
            quotes=tuple(quotes),
        )

    def fetch_contract_window(
        self,
        *,
        symbol: str,
        expiration_date: date,
        contract: ProviderListedContract,
        start_at: datetime,
        end_at: datetime,
    ) -> ThetaContractQuoteWindow:
        """Fetch a bounded diagnostic window for one supplied contract.

        This method never discovers or broadens contract identity. Production
        ``acquire_quotes`` uses it with a single completed minute.
        """

        requested_symbol = symbol.strip().upper()
        if not requested_symbol:
            raise ValueError("symbol is required")
        _validate_contract(contract, expiration_date)
        start_local = _minute_aligned(start_at, "start_at")
        end_local = _minute_aligned(end_at, "end_at")
        if end_local < start_local:
            raise ValueError("end_at cannot precede start_at")
        if start_local.date() != end_local.date():
            raise ValueError("quote window must remain within one session date")

        request = {
            "symbol": requested_symbol,
            "expiration": expiration_date,
            "interval": "1m",
            "date": start_local.date(),
            "strike": _strike_text(contract.strike),
            "right": contract.right.lower(),
            "start_time": _start_time(start_local),
            "end_time": _end_time(end_local),
        }
        try:
            frame = self._client.option_history_quote(**request)
        except NoDataFoundError:
            return ThetaContractQuoteWindow(
                symbol=requested_symbol,
                expiration_date=expiration_date,
                start_at=start_at,
                end_at=end_at,
                contract=contract,
                acquisition_succeeded=False,
                quotes=(),
            )
        except AuthenticationError as exc:
            raise ThetaQuoteAuthenticationError("ThetaData authentication failed") from exc
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise ThetaQuoteAuthenticationError(
                    "ThetaData rejected the authenticated session"
                ) from exc
            if exc.code() == grpc.StatusCode.PERMISSION_DENIED:
                raise ThetaQuoteEntitlementError(
                    "ThetaData rejected historical option quote access"
                ) from exc
            raise ThetaQuoteTransportError("ThetaData quote request failed") from exc
        except Exception as exc:
            raise ThetaQuoteTransportError("ThetaData quote request failed") from exc

        rows = _frame_rows(frame)
        if not rows:
            return ThetaContractQuoteWindow(
                symbol=requested_symbol,
                expiration_date=expiration_date,
                start_at=start_at,
                end_at=end_at,
                contract=contract,
                acquisition_succeeded=False,
                quotes=(),
            )
        quotes = tuple(
            _map_row(
                row,
                symbol=requested_symbol,
                expiration_date=expiration_date,
                contract=contract,
                start_at=start_local,
                end_at=end_local,
            )
            for row in rows
        )
        timestamps = tuple(quote.provider_timestamp for quote in quotes)
        if len(set(timestamps)) != len(timestamps):
            raise ThetaQuoteSchemaError("ThetaData returned duplicate quote timestamps")
        return ThetaContractQuoteWindow(
            symbol=requested_symbol,
            expiration_date=expiration_date,
            start_at=start_at,
            end_at=end_at,
            contract=contract,
            acquisition_succeeded=True,
            quotes=tuple(sorted(quotes, key=lambda quote: quote.provider_timestamp)),
        )


def _validate_request_context(
    *,
    symbol: str,
    completed_at: datetime,
    expiration_date: date,
    contracts: tuple[ProviderListedContract, ...],
) -> str:
    requested_symbol = symbol.strip().upper()
    if not requested_symbol:
        raise ValueError("symbol is required")
    _minute_aligned(completed_at, "completed_at")
    identities = [contract.contract_id for contract in contracts]
    if len(set(identities)) != len(identities):
        raise ValueError("requested contracts must have unique identities")
    for contract in contracts:
        _validate_contract(contract, expiration_date)
    return requested_symbol


def _validate_contract(contract: ProviderListedContract, expiration_date: date) -> None:
    if contract.expiration_date != expiration_date:
        raise ValueError("requested contract expiration contradicts request")
    if contract.right not in ("CALL", "PUT"):
        raise ValueError("requested contract right must be CALL or PUT")


def _contract_order_key(contract: ProviderListedContract) -> tuple[Any, ...]:
    return (
        contract.expiration_date,
        contract.strike,
        contract.right,
        contract.contract_id,
    )


def _minute_aligned(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    local = value.astimezone(NEW_YORK)
    if local.second != 0 or local.microsecond != 0:
        raise ValueError(f"{field} must identify an exact one-minute interval")
    return local


def _start_time(value: datetime) -> time:
    return time(value.hour, value.minute)


def _end_time(value: datetime) -> time:
    return time(value.hour, value.minute, 59, 999000)


def _strike_text(value: Decimal) -> str:
    return format(value, "f")


def _frame_rows(frame: Any) -> tuple[dict[str, Any], ...]:
    columns = set(getattr(frame, "columns", ()))
    missing = REQUIRED_FIELDS - columns
    if missing:
        raise ThetaQuoteSchemaError(
            f"ThetaData quote response is missing fields: {sorted(missing)}"
        )
    try:
        return tuple(dict(row) for row in frame.to_dicts())
    except Exception as exc:
        raise ThetaQuoteSchemaError("ThetaData quote response cannot be decoded") from exc


def _map_row(
    row: dict[str, Any],
    *,
    symbol: str,
    expiration_date: date,
    contract: ProviderListedContract,
    start_at: datetime,
    end_at: datetime,
) -> ThetaProviderQuote:
    row_symbol = str(row["symbol"]).upper()
    row_expiration = _expiration(row["expiration"])
    row_strike = _decimal(row["strike"], "strike")
    row_right = str(row["right"]).upper()
    if (
        row_symbol != symbol
        or row_expiration != expiration_date
        or row_strike != contract.strike
        or row_right != contract.right
    ):
        raise ThetaQuoteIdentityError("ThetaData quote identity contradicts request")
    timestamp = row["timestamp"]
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
        raise ThetaQuoteSchemaError("ThetaData quote timestamp must be timezone-aware")
    timestamp_local = timestamp.astimezone(NEW_YORK)
    if not (start_at <= timestamp_local <= end_at):
        raise ThetaQuoteTimestampError("ThetaData quote timestamp is outside request")
    raw_bid = row["bid"]
    raw_ask = row["ask"]
    return ThetaProviderQuote(
        contract_id=contract.contract_id,
        expiration_date=expiration_date,
        strike=contract.strike,
        right=contract.right,
        bid=_finite_decimal_or_none(raw_bid),
        ask=_finite_decimal_or_none(raw_ask),
        provider_timestamp=timestamp,
        raw_bid=raw_bid,
        raw_ask=raw_ask,
        bid_size=row["bid_size"],
        ask_size=row["ask_size"],
        bid_exchange=row["bid_exchange"],
        ask_exchange=row["ask_exchange"],
        bid_condition=row["bid_condition"],
        ask_condition=row["ask_condition"],
    )


def _expiration(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ThetaQuoteSchemaError("ThetaData expiration is invalid") from exc


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ThetaQuoteSchemaError(f"ThetaData {field} is invalid") from exc
    if not result.is_finite():
        raise ThetaQuoteSchemaError(f"ThetaData {field} must be finite")
    return result


def _finite_decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None
