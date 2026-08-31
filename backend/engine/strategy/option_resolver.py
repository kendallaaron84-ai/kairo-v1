from datetime import date
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import OptionRight
from app.domain.instruments import CanonicalInstrument


PREMIUM_CAP = Decimal("0.50")
MAX_BID_ASK_SPREAD = Decimal("0.03")
MINIMUM_VOLUME = 10
MINIMUM_OPEN_INTEREST = 50


class OptionContractCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_id: UUID
    underlying_symbol: str = Field(min_length=1)
    expiration_date: date
    strike_price: Decimal = Field(gt=0)
    option_right: OptionRight
    contract_symbol: str | None = None
    contract_multiplier: Decimal = Field(gt=0)
    listing_type: str = Field(default="STANDARD", min_length=1)
    bid: Decimal
    ask: Decimal
    volume: int = Field(ge=0)
    open_interest: int = Field(ge=0)

    @model_validator(mode="after")
    def contract_symbol_is_not_blank(self) -> "OptionContractCandidate":
        if self.contract_symbol is not None and not self.contract_symbol.strip():
            raise ValueError("contract symbol cannot be blank")
        return self


class ResolvedOptionContract(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_id: UUID
    symbol: str = Field(min_length=1)
    underlying_symbol: str
    expiration_date: date
    strike_price: Decimal
    option_right: OptionRight
    contract_symbol: str | None
    contract_multiplier: Decimal = Field(gt=0)
    listing_type: str
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)
    volume: int = Field(ge=0)
    open_interest: int = Field(ge=0)


class CanonicalInstrumentLookup(Protocol):
    def get(self, instrument_id: UUID) -> CanonicalInstrument | None: ...


class MappingInstrumentLookup:
    """Small canonical lookup useful for replay composition and deterministic tests."""

    def __init__(self, instruments: tuple[CanonicalInstrument, ...]) -> None:
        self._instruments = {item.instrument_id: item for item in instruments}

    def get(self, instrument_id: UUID) -> CanonicalInstrument | None:
        instrument = self._instruments.get(instrument_id)
        if instrument is None or instrument.retired_at is not None:
            return None
        return instrument


def select_expiration(expirations: tuple[date, ...], session_date: date) -> date:
    eligible = sorted(expiration for expiration in set(expirations) if expiration >= session_date)
    if not eligible:
        raise ValueError("no upcoming option expiration is available")
    return session_date if session_date in eligible else eligible[0]


class LegacySessionExpirationResolver:
    """Resolves and freezes one expiration per underlying for a legacy session."""

    def __init__(self, *, session_date: date) -> None:
        self.session_date = session_date
        self._resolved: dict[str, date] = {}

    def resolve(self, underlying_symbol: str, expirations: tuple[date, ...]) -> date:
        if underlying_symbol not in self._resolved:
            self._resolved[underlying_symbol] = select_expiration(
                expirations, self.session_date
            )
        return self._resolved[underlying_symbol]


def is_legacy_eligible(contract: OptionContractCandidate) -> bool:
    return (
        contract.bid > 0
        and contract.ask > 0
        and contract.ask <= PREMIUM_CAP
        and contract.ask - contract.bid <= MAX_BID_ASK_SPREAD
        and (
            contract.volume >= MINIMUM_VOLUME
            or contract.open_interest >= MINIMUM_OPEN_INTEREST
        )
    )


def resolve_legacy_option(
    *,
    candidates: tuple[OptionContractCandidate, ...],
    underlying_symbol: str,
    expiration_date: date,
    option_right: OptionRight,
    spot_price: Decimal,
    canonical_lookup: CanonicalInstrumentLookup,
) -> ResolvedOptionContract | None:
    if spot_price <= 0:
        raise ValueError("underlying spot must be positive")
    eligible = [
        contract
        for contract in candidates
        if contract.underlying_symbol == underlying_symbol
        and contract.expiration_date == expiration_date
        and contract.option_right == option_right
        and is_legacy_eligible(contract)
    ]
    if not eligible:
        return None
    chosen = min(eligible, key=lambda contract: abs(contract.strike_price - spot_price))
    canonical = canonical_lookup.get(chosen.instrument_id)
    if canonical is None:
        raise ValueError("selected option is absent or retired in the canonical instrument registry")
    validate_candidate_identity(chosen, canonical)
    return ResolvedOptionContract(
        instrument_id=canonical.instrument_id,
        symbol=canonical.symbol,
        underlying_symbol=canonical.underlying_symbol,
        expiration_date=canonical.expiration_date,
        strike_price=canonical.strike_price,
        option_right=canonical.option_right,
        contract_symbol=canonical.contract_symbol,
        contract_multiplier=canonical.contract_multiplier,
        listing_type=canonical.listing_type,
        bid=chosen.bid,
        ask=chosen.ask,
        volume=chosen.volume,
        open_interest=chosen.open_interest,
    )


def validate_candidate_identity(
    candidate: OptionContractCandidate, canonical: CanonicalInstrument
) -> None:
    if canonical.asset_class != "OPTION":
        raise ValueError("selected canonical instrument is not an option")
    comparisons = {
        "underlying_symbol": (candidate.underlying_symbol, canonical.underlying_symbol),
        "expiration_date": (candidate.expiration_date, canonical.expiration_date),
        "strike_price": (candidate.strike_price, canonical.strike_price),
        "option_right": (candidate.option_right, canonical.option_right),
        "contract_symbol": (candidate.contract_symbol, canonical.contract_symbol),
        "contract_multiplier": (
            candidate.contract_multiplier,
            canonical.contract_multiplier,
        ),
        "listing_type": (candidate.listing_type, canonical.listing_type),
    }
    mismatches = [
        name
        for name, (observed, truth) in comparisons.items()
        if observed != truth
    ]
    if mismatches:
        raise ValueError(
            "candidate option identity conflicts with canonical instrument: "
            + ", ".join(mismatches)
        )
