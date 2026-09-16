"""In-memory dynamic option evidence construction.

Listing discovery and quote acquisition are intentionally separate provider calls.
The slicer selects the provider-ordinal request envelope, preserves returned evidence,
and delegates every qualification decision to DYNAMIC-ENVELOPE-CONTRACT-v1.0's
production qualifier.  It performs no provider transport or persistence.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from engine.validation.dynamic_envelope_qualifier import (
    CONTRACT_ID,
    DynamicEnvelopeQualifier,
    IntervalQualification,
    build_dynamic_envelope_qualifier,
)


OptionRight = Literal["CALL", "PUT"]
NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class CompletedUnderlyingBar:
    symbol: str
    completed_at: datetime
    close: Decimal

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("underlying symbol is required")
        if self.completed_at.tzinfo is None:
            raise ValueError("completed_at must be timezone-aware")
        if self.close <= 0:
            raise ValueError("underlying close must be positive")


@dataclass(frozen=True)
class ProviderListedContract:
    contract_id: str
    expiration_date: date
    strike: Decimal
    right: OptionRight
    quote_expected: bool = True

    def __post_init__(self) -> None:
        if not self.contract_id:
            raise ValueError("provider contract identity is required")
        if self.strike <= 0:
            raise ValueError("provider-listed strike must be positive")
        if self.right not in ("CALL", "PUT"):
            raise ValueError("option right must be CALL or PUT")


@dataclass(frozen=True)
class ProviderExpirationListing:
    expiration_date: date
    strikes: tuple[Decimal, ...]
    contracts: tuple[ProviderListedContract, ...]


@dataclass(frozen=True)
class ProviderListingSnapshot:
    symbol: str
    timestamp: datetime
    discovery_succeeded: bool
    expirations: tuple[ProviderExpirationListing, ...]


@dataclass(frozen=True)
class ProviderQuote:
    contract_id: str
    expiration_date: date
    strike: Decimal
    right: OptionRight
    bid: Decimal | None
    ask: Decimal | None


@dataclass(frozen=True)
class ProviderQuoteSnapshot:
    symbol: str
    expiration_date: date
    timestamp: datetime
    acquisition_succeeded: bool
    quotes: tuple[ProviderQuote, ...]


class DynamicOptionEvidenceProvider(Protocol):
    """Independent listing and quote acquisition boundary."""

    def discover_listings(
        self,
        *,
        symbol: str,
        completed_at: datetime,
    ) -> ProviderListingSnapshot: ...

    def acquire_quotes(
        self,
        *,
        symbol: str,
        completed_at: datetime,
        expiration_date: date,
        contracts: tuple[ProviderListedContract, ...],
    ) -> ProviderQuoteSnapshot: ...


@dataclass(frozen=True)
class ExpectedUniverseEvidence:
    timestamp: datetime
    strikes: tuple[Decimal, ...]
    contracts: tuple[ProviderListedContract, ...]


@dataclass(frozen=True)
class AcquiredContractEvidence:
    contract_id: str
    strike: Decimal
    right: OptionRight
    bid: Decimal | None
    ask: Decimal | None


@dataclass(frozen=True)
class AcquiredUniverseEvidence:
    timestamp: datetime
    contracts: tuple[AcquiredContractEvidence, ...]


@dataclass(frozen=True)
class DynamicEnvelopeQualificationRequest:
    contract_id: str
    underlying_completed_at: datetime
    spot: Decimal
    expected_universe: ExpectedUniverseEvidence
    acquired_universe: AcquiredUniverseEvidence


@dataclass(frozen=True)
class DynamicEnvelopeSlice:
    symbol: str
    completed_at: datetime
    spot: Decimal
    expiration_date: date | None
    listing_discovery_succeeded: bool
    quote_acquisition_succeeded: bool | None
    requested_contract_ids: tuple[str, ...]
    qualification_request: DynamicEnvelopeQualificationRequest
    qualification: IntervalQualification

    @property
    def expected_universe(self) -> ExpectedUniverseEvidence:
        return self.qualification_request.expected_universe

    @property
    def acquired_universe(self) -> AcquiredUniverseEvidence:
        return self.qualification_request.acquired_universe


class DynamicOptionEvidenceSlicer:
    """Build dynamic evidence at each completed bar and invoke the frozen qualifier."""

    def __init__(
        self,
        provider: DynamicOptionEvidenceProvider,
        qualifier: DynamicEnvelopeQualifier | None = None,
    ) -> None:
        self._provider = provider
        self._qualifier = qualifier or build_dynamic_envelope_qualifier()

    def slice_bars(
        self,
        bars: tuple[CompletedUnderlyingBar, ...],
    ) -> tuple[DynamicEnvelopeSlice, ...]:
        if any(
            current.completed_at <= previous.completed_at
            for previous, current in zip(bars, bars[1:])
        ):
            raise ValueError("completed bars must be strictly chronological")
        return tuple(result for bar in bars for result in self.slice_bar(bar))

    def slice_bar(self, bar: CompletedUnderlyingBar) -> tuple[DynamicEnvelopeSlice, ...]:
        symbol = bar.symbol.upper()
        listing_snapshot = self._provider.discover_listings(
            symbol=symbol,
            completed_at=bar.completed_at,
        )
        self._validate_listing_snapshot(listing_snapshot, symbol, bar.completed_at)
        if not listing_snapshot.discovery_succeeded:
            if listing_snapshot.expirations:
                raise ValueError("failed listing discovery cannot claim expiration evidence")
            return (self._unavailable_slice(bar, symbol, listing_snapshot),)

        session_date = bar.completed_at.astimezone(NEW_YORK).date()
        active_expirations = tuple(
            expiration
            for expiration in self._ordered_expirations(listing_snapshot.expirations)
            if 0 <= (expiration.expiration_date - session_date).days <= 5
        )
        if not active_expirations:
            return (self._unavailable_slice(bar, symbol, listing_snapshot),)
        return tuple(
            self._slice_expiration(bar, symbol, listing_snapshot, expiration)
            for expiration in active_expirations
        )

    def _slice_expiration(
        self,
        bar: CompletedUnderlyingBar,
        symbol: str,
        listing_snapshot: ProviderListingSnapshot,
        expiration: ProviderExpirationListing,
    ) -> DynamicEnvelopeSlice:
        strikes = self._ordered_strikes(expiration.strikes)
        contracts = self._ordered_contracts(expiration, strikes)
        expected_universe = ExpectedUniverseEvidence(
            timestamp=listing_snapshot.timestamp,
            strikes=strikes,
            contracts=contracts,
        )
        required_strikes = self._required_strikes(strikes, bar.close)
        if required_strikes is None:
            acquired_universe = AcquiredUniverseEvidence(
                timestamp=bar.completed_at,
                contracts=(),
            )
            return self._qualify_slice(
                bar=bar,
                symbol=symbol,
                expiration_date=expiration.expiration_date,
                listing_discovery_succeeded=True,
                quote_acquisition_succeeded=None,
                requested_contract_ids=(),
                expected_universe=expected_universe,
                acquired_universe=acquired_universe,
            )

        required_set = set(required_strikes)
        requested_contracts = tuple(
            contract for contract in contracts if contract.strike in required_set
        )
        quote_snapshot = self._provider.acquire_quotes(
            symbol=symbol,
            completed_at=bar.completed_at,
            expiration_date=expiration.expiration_date,
            contracts=requested_contracts,
        )
        self._validate_quote_snapshot(
            quote_snapshot,
            symbol=symbol,
            expiration_date=expiration.expiration_date,
            requested_contracts=requested_contracts,
        )
        acquired_universe = AcquiredUniverseEvidence(
            timestamp=quote_snapshot.timestamp,
            contracts=tuple(
                AcquiredContractEvidence(
                    contract_id=quote.contract_id,
                    strike=quote.strike,
                    right=quote.right,
                    bid=quote.bid,
                    ask=quote.ask,
                )
                for quote in self._ordered_quotes(quote_snapshot.quotes)
            ),
        )
        return self._qualify_slice(
            bar=bar,
            symbol=symbol,
            expiration_date=expiration.expiration_date,
            listing_discovery_succeeded=True,
            quote_acquisition_succeeded=quote_snapshot.acquisition_succeeded,
            requested_contract_ids=tuple(
                contract.contract_id for contract in requested_contracts
            ),
            expected_universe=expected_universe,
            acquired_universe=acquired_universe,
        )

    def _unavailable_slice(
        self,
        bar: CompletedUnderlyingBar,
        symbol: str,
        listing_snapshot: ProviderListingSnapshot,
    ) -> DynamicEnvelopeSlice:
        return self._qualify_slice(
            bar=bar,
            symbol=symbol,
            expiration_date=None,
            listing_discovery_succeeded=listing_snapshot.discovery_succeeded,
            quote_acquisition_succeeded=None,
            requested_contract_ids=(),
            expected_universe=ExpectedUniverseEvidence(
                timestamp=listing_snapshot.timestamp,
                strikes=(),
                contracts=(),
            ),
            acquired_universe=AcquiredUniverseEvidence(
                timestamp=bar.completed_at,
                contracts=(),
            ),
        )

    def _qualify_slice(
        self,
        *,
        bar: CompletedUnderlyingBar,
        symbol: str,
        expiration_date: date | None,
        listing_discovery_succeeded: bool,
        quote_acquisition_succeeded: bool | None,
        requested_contract_ids: tuple[str, ...],
        expected_universe: ExpectedUniverseEvidence,
        acquired_universe: AcquiredUniverseEvidence,
    ) -> DynamicEnvelopeSlice:
        request = DynamicEnvelopeQualificationRequest(
            contract_id=CONTRACT_ID,
            underlying_completed_at=bar.completed_at,
            spot=bar.close,
            expected_universe=expected_universe,
            acquired_universe=acquired_universe,
        )
        qualification = self._qualifier.qualify_interval(request)
        return DynamicEnvelopeSlice(
            symbol=symbol,
            completed_at=bar.completed_at,
            spot=bar.close,
            expiration_date=expiration_date,
            listing_discovery_succeeded=listing_discovery_succeeded,
            quote_acquisition_succeeded=quote_acquisition_succeeded,
            requested_contract_ids=requested_contract_ids,
            qualification_request=request,
            qualification=qualification,
        )

    @staticmethod
    def _validate_listing_snapshot(
        snapshot: ProviderListingSnapshot,
        symbol: str,
        completed_at: datetime,
    ) -> None:
        if snapshot.symbol.upper() != symbol:
            raise ValueError("listing snapshot symbol contradicts the completed bar")
        if snapshot.timestamp != completed_at:
            raise ValueError("listing discovery timestamp must equal completed_at")

    @staticmethod
    def _ordered_expirations(
        expirations: tuple[ProviderExpirationListing, ...],
    ) -> tuple[ProviderExpirationListing, ...]:
        dates = tuple(expiration.expiration_date for expiration in expirations)
        if len(set(dates)) != len(dates):
            raise ValueError("provider listing contains duplicate expirations")
        return tuple(sorted(expirations, key=lambda expiration: expiration.expiration_date))

    @staticmethod
    def _ordered_strikes(strikes: tuple[Decimal, ...]) -> tuple[Decimal, ...]:
        if len(set(strikes)) != len(strikes):
            raise ValueError("provider listing contains duplicate strikes")
        return tuple(sorted(strikes))

    @staticmethod
    def _ordered_contracts(
        expiration: ProviderExpirationListing,
        strikes: tuple[Decimal, ...],
    ) -> tuple[ProviderListedContract, ...]:
        strike_set = set(strikes)
        identities: set[tuple[str, Decimal, str]] = set()
        ordered = tuple(
            sorted(
                expiration.contracts,
                key=lambda contract: (
                    contract.strike,
                    contract.right,
                    contract.contract_id,
                ),
            )
        )
        for contract in ordered:
            if contract.expiration_date != expiration.expiration_date:
                raise ValueError("contract expiration contradicts its listing group")
            if contract.strike not in strike_set:
                raise ValueError("contract strike is absent from provider strike reality")
            identity = (contract.contract_id, contract.strike, contract.right)
            if identity in identities:
                raise ValueError("provider listing contains duplicate contract identity")
            identities.add(identity)
        return ordered

    @staticmethod
    def _required_strikes(
        strikes: tuple[Decimal, ...],
        spot: Decimal,
    ) -> tuple[Decimal, ...] | None:
        if not strikes:
            return None
        atm = min(strikes, key=lambda strike: (abs(strike - spot), strike))
        index = strikes.index(atm)
        if index < 10 or len(strikes) - index - 1 < 10:
            return None
        return strikes[index - 10 : index + 11]

    @staticmethod
    def _validate_quote_snapshot(
        snapshot: ProviderQuoteSnapshot,
        *,
        symbol: str,
        expiration_date: date,
        requested_contracts: tuple[ProviderListedContract, ...],
    ) -> None:
        if snapshot.symbol.upper() != symbol:
            raise ValueError("quote snapshot symbol contradicts the completed bar")
        if snapshot.expiration_date != expiration_date:
            raise ValueError("quote snapshot expiration contradicts the request")
        expected_by_id = {contract.contract_id: contract for contract in requested_contracts}
        observed_ids: set[str] = set()
        for quote in snapshot.quotes:
            if quote.contract_id in observed_ids:
                raise ValueError("quote response contains duplicate contract identity")
            observed_ids.add(quote.contract_id)
            expected = expected_by_id.get(quote.contract_id)
            if expected is None:
                raise ValueError("quote response contains an unrequested contract")
            if (
                quote.expiration_date != expected.expiration_date
                or quote.strike != expected.strike
                or quote.right != expected.right
            ):
                raise ValueError("quote response contradicts provider-listed identity")

    @staticmethod
    def _ordered_quotes(quotes: tuple[ProviderQuote, ...]) -> tuple[ProviderQuote, ...]:
        return tuple(
            sorted(
                quotes,
                key=lambda quote: (quote.strike, quote.right, quote.contract_id),
            )
        )
