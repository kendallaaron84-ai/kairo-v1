from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import OptionRight


PREMIUM_CAP = Decimal("0.50")
MAX_BID_ASK_SPREAD = Decimal("0.03")
MINIMUM_VOLUME = 10
MINIMUM_OPEN_INTEREST = 50


class OptionContractCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

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
    return ResolvedOptionContract(
        underlying_symbol=chosen.underlying_symbol,
        expiration_date=chosen.expiration_date,
        strike_price=chosen.strike_price,
        option_right=chosen.option_right,
        contract_symbol=chosen.contract_symbol,
        contract_multiplier=chosen.contract_multiplier,
        listing_type=chosen.listing_type,
        bid=chosen.bid,
        ask=chosen.ask,
        volume=chosen.volume,
        open_interest=chosen.open_interest,
    )
