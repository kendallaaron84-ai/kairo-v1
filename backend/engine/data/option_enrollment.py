"""Governed canonical enrollment for provider-discovered research option contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models.configuration import Instrument
from app.domain.enums import OptionRight
from app.repositories.configuration import InstrumentRepository


KAIRO_INSTRUMENT_NAMESPACE = uuid5(NAMESPACE_URL, "kairo:canonical-instrument:v1")
STANDARD_MULTIPLIER = Decimal("100")


class OptionEnrollmentReasonCode(StrEnum):
    NON_POSITIVE_STRIKE = "NON_POSITIVE_STRIKE"
    UNPARSEABLE_STRIKE = "UNPARSEABLE_STRIKE"
    INVALID_OPTION_RIGHT = "INVALID_OPTION_RIGHT"
    INVALID_EXPIRATION = "INVALID_EXPIRATION"
    MISSING_CAUSAL_OBSERVATION = "MISSING_CAUSAL_OBSERVATION"
    UNDERLYING_MISMATCH = "UNDERLYING_MISMATCH"
    UNDERLYING_NOT_ENROLLED = "UNDERLYING_NOT_ENROLLED"
    RESEARCH_REPLAY_MODE_REQUIRED = "RESEARCH_REPLAY_MODE_REQUIRED"
    CANONICAL_ATTRIBUTE_CONFLICT = "CANONICAL_ATTRIBUTE_CONFLICT"
    CANONICAL_LOOKUP_FAILED = "CANONICAL_LOOKUP_FAILED"


class OptionEnrollmentFailure(ValueError):
    def __init__(self, reason_code: OptionEnrollmentReasonCode, diagnostic: str) -> None:
        self.reason_code = reason_code
        self.diagnostic = diagnostic
        super().__init__(reason_code.value)


class RejectedOptionContract(BaseModel):
    model_config = ConfigDict(frozen=True)

    discovery_key: str
    reason_code: OptionEnrollmentReasonCode
    diagnostic: str | None = None


class CanonicalResolutionAccounting(BaseModel):
    model_config = ConfigDict(frozen=True)

    discovered_contracts_count: int = Field(ge=0)
    resolved_existing_contracts_count: int = Field(ge=0)
    newly_enrolled_contracts_count: int = Field(ge=0)
    resolved_contracts_count: int = Field(ge=0)
    rejected_contracts_count: int = Field(ge=0)
    rejected_contracts: tuple[RejectedOptionContract, ...] = ()

    @model_validator(mode="after")
    def population_is_conserved(self) -> "CanonicalResolutionAccounting":
        if self.resolved_contracts_count != (
            self.resolved_existing_contracts_count + self.newly_enrolled_contracts_count
        ):
            raise ValueError("resolution accounting resolved population mismatch")
        if self.rejected_contracts_count != len(self.rejected_contracts):
            raise ValueError("resolution accounting rejected population mismatch")
        if self.discovered_contracts_count != (
            self.resolved_contracts_count + self.rejected_contracts_count
        ):
            raise ValueError("resolution accounting discovered population mismatch")
        return self

    @property
    def resolution_percentage(self) -> Decimal:
        if self.discovered_contracts_count == 0:
            return Decimal("0.00")
        return (
            Decimal(self.resolved_contracts_count)
            * Decimal("100")
            / Decimal(self.discovered_contracts_count)
        ).quantize(Decimal("0.01"))

    @classmethod
    def combine(
        cls, values: Sequence["CanonicalResolutionAccounting"]
    ) -> "CanonicalResolutionAccounting":
        rejected = tuple(sorted(
            (item for value in values for item in value.rejected_contracts),
            key=lambda item: (item.discovery_key, item.reason_code.value),
        ))
        existing = sum(value.resolved_existing_contracts_count for value in values)
        enrolled = sum(value.newly_enrolled_contracts_count for value in values)
        return cls(
            discovered_contracts_count=sum(value.discovered_contracts_count for value in values),
            resolved_existing_contracts_count=existing,
            newly_enrolled_contracts_count=enrolled,
            resolved_contracts_count=existing + enrolled,
            rejected_contracts_count=len(rejected),
            rejected_contracts=rejected,
        )


class OptionEnrollmentOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    accounting: CanonicalResolutionAccounting
    accepted_contract_keys: frozenset[tuple[date, Decimal, OptionRight]]


def deterministic_option_instrument_id(
    underlying_symbol: str,
    expiration: date,
    strike: Decimal,
    right: OptionRight | str,
) -> UUID:
    normalized_right = right.value if isinstance(right, OptionRight) else str(right).upper()
    canonical_key = (
        f"OPTION|{underlying_symbol.upper()}|{expiration.isoformat()}|"
        f"{strike:.6f}|{normalized_right}|STANDARD"
    )
    return uuid5(KAIRO_INSTRUMENT_NAMESPACE, canonical_key)


def canonical_occ_symbol(
    underlying_symbol: str,
    expiration: date,
    strike: Decimal,
    right: OptionRight,
) -> str:
    strike_mills = strike * Decimal("1000")
    if strike_mills != strike_mills.to_integral_value() or strike_mills < 0:
        raise OptionEnrollmentFailure(
            OptionEnrollmentReasonCode.UNPARSEABLE_STRIKE,
            "strike cannot be represented by the canonical OCC mill encoding",
        )
    right_code = "C" if right is OptionRight.CALL else "P"
    return (
        f"{underlying_symbol.upper()}{expiration:%y%m%d}{right_code}"
        f"{int(strike_mills):08d}"
    )


class HistoricalOptionEnrollmentGate:
    """Enroll exact provider discoveries only within research replay mode."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = InstrumentRepository(session)

    def enroll_theta_sections(
        self,
        sections: Sequence[Any],
        *,
        underlying_instrument_id: UUID,
        underlying_symbol: str,
        research_replay_mode: bool,
    ) -> OptionEnrollmentOutcome:
        discoveries = [
            {
                "symbol": section.parameters.get("symbol", underlying_symbol),
                "expiration": section.parameters.get("expiration"),
                **row,
            }
            for section in sections
            if section.endpoint == "option_history_quote"
            for row in section.records
        ]
        return self.enroll(
            discoveries,
            underlying_instrument_id=underlying_instrument_id,
            underlying_symbol=underlying_symbol,
            research_replay_mode=research_replay_mode,
        )

    def enroll(
        self,
        discoveries: Sequence[Mapping[str, Any]],
        *,
        underlying_instrument_id: UUID,
        underlying_symbol: str,
        research_replay_mode: bool,
    ) -> OptionEnrollmentOutcome:
        if not research_replay_mode:
            raise OptionEnrollmentFailure(
                OptionEnrollmentReasonCode.RESEARCH_REPLAY_MODE_REQUIRED,
                "historical option enrollment is restricted to research replay",
            )
        underlying = self.session.get(Instrument, underlying_instrument_id)
        if (
            underlying is None
            or underlying.retired_at is not None
            or underlying.asset_class not in {"EQUITY", "ETF"}
            or underlying.symbol != underlying_symbol
        ):
            raise OptionEnrollmentFailure(
                OptionEnrollmentReasonCode.UNDERLYING_NOT_ENROLLED,
                "approved active underlying instrument is absent",
            )

        valid: dict[tuple[date, Decimal, OptionRight], datetime] = {}
        rejected: dict[str, RejectedOptionContract] = {}
        for raw in discoveries:
            discovery_key = self._discovery_key(raw)
            try:
                key, observed_at = self._parse(raw, underlying_symbol)
            except OptionEnrollmentFailure as error:
                rejected.setdefault(discovery_key, RejectedOptionContract(
                    discovery_key=discovery_key,
                    reason_code=error.reason_code,
                    diagnostic=error.diagnostic,
                ))
                continue
            prior = valid.get(key)
            if prior is None or observed_at < prior:
                valid[key] = observed_at

        existing_count = 0
        enrolled_count = 0
        accepted: set[tuple[date, Decimal, OptionRight]] = set()
        for key, observed_at in sorted(valid.items(), key=lambda item: (
            item[0][0], item[0][1], item[0][2].value
        )):
            expiration, strike, right = key
            instrument_id = deterministic_option_instrument_id(
                underlying_symbol, expiration, strike, right
            )
            symbol = canonical_occ_symbol(underlying_symbol, expiration, strike, right)
            matches = self.session.scalars(select(Instrument).where(or_(
                Instrument.instrument_id == instrument_id,
                (
                    (Instrument.asset_class == "OPTION")
                    & (Instrument.underlying_symbol == underlying_symbol)
                    & (Instrument.expiration_date == expiration)
                    & (Instrument.strike_price == strike)
                    & (Instrument.option_right == right.value)
                ),
            ))).all()
            if len(matches) > 1:
                self._conflict("derived UUID and canonical tuple resolve to different rows")
            if matches:
                self._validate_existing(
                    matches[0], instrument_id, symbol, underlying_symbol, key
                )
                existing_count += 1
            else:
                self.repository.add(Instrument(
                    instrument_id=instrument_id,
                    symbol=symbol,
                    asset_class="OPTION",
                    currency="USD",
                    underlying_symbol=underlying_symbol,
                    contract_symbol=symbol,
                    expiration_date=expiration,
                    strike_price=strike,
                    option_right=right.value,
                    contract_multiplier=STANDARD_MULTIPLIER,
                    listing_type="STANDARD",
                    effective_from=observed_at,
                ))
                enrolled_count += 1
            accepted.add(key)

        ordered_rejected = tuple(sorted(
            rejected.values(), key=lambda item: (item.discovery_key, item.reason_code.value)
        ))
        accounting = CanonicalResolutionAccounting(
            discovered_contracts_count=len(valid) + len(ordered_rejected),
            resolved_existing_contracts_count=existing_count,
            newly_enrolled_contracts_count=enrolled_count,
            resolved_contracts_count=existing_count + enrolled_count,
            rejected_contracts_count=len(ordered_rejected),
            rejected_contracts=ordered_rejected,
        )
        return OptionEnrollmentOutcome(
            accounting=accounting,
            accepted_contract_keys=frozenset(accepted),
        )

    @staticmethod
    def _parse(
        raw: Mapping[str, Any], underlying_symbol: str
    ) -> tuple[tuple[date, Decimal, OptionRight], datetime]:
        if str(raw.get("symbol", underlying_symbol)).upper() != underlying_symbol.upper():
            raise OptionEnrollmentFailure(
                OptionEnrollmentReasonCode.UNDERLYING_MISMATCH,
                "provider contract underlying differs from the approved underlying",
            )
        try:
            strike = Decimal(str(raw.get("strike")))
        except (InvalidOperation, ValueError):
            raise OptionEnrollmentFailure(
                OptionEnrollmentReasonCode.UNPARSEABLE_STRIKE,
                "provider strike is not an exact decimal",
            ) from None
        if not strike.is_finite():
            raise OptionEnrollmentFailure(
                OptionEnrollmentReasonCode.UNPARSEABLE_STRIKE,
                "provider strike is not a finite exact decimal",
            )
        if strike <= 0:
            raise OptionEnrollmentFailure(
                OptionEnrollmentReasonCode.NON_POSITIVE_STRIKE,
                "provider strike must be strictly positive",
            )
        if strike * Decimal("1000") != (strike * Decimal("1000")).to_integral_value():
            raise OptionEnrollmentFailure(
                OptionEnrollmentReasonCode.UNPARSEABLE_STRIKE,
                "provider strike exceeds canonical OCC mill precision",
            )
        try:
            expiration_value = raw["expiration"]
            expiration = (
                expiration_value.date()
                if isinstance(expiration_value, datetime)
                else expiration_value
                if isinstance(expiration_value, date)
                else date.fromisoformat(str(expiration_value)[:10])
            )
        except (KeyError, TypeError, ValueError):
            raise OptionEnrollmentFailure(
                OptionEnrollmentReasonCode.INVALID_EXPIRATION,
                "provider expiration is absent or invalid",
            ) from None
        raw_right = str(raw.get("right", "")).upper()
        if raw_right in {"CALL", "C"}:
            right = OptionRight.CALL
        elif raw_right in {"PUT", "P"}:
            right = OptionRight.PUT
        else:
            raise OptionEnrollmentFailure(
                OptionEnrollmentReasonCode.INVALID_OPTION_RIGHT,
                "provider option right is absent or invalid",
            )
        observed_at = raw.get("timestamp")
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            raise OptionEnrollmentFailure(
                OptionEnrollmentReasonCode.MISSING_CAUSAL_OBSERVATION,
                "provider contract lacks a timezone-aware observation timestamp",
            )
        return (expiration, strike, right), observed_at

    @staticmethod
    def _discovery_key(raw: Mapping[str, Any]) -> str:
        return "|".join((
            str(raw.get("symbol", "")),
            str(raw.get("expiration", "")),
            str(raw.get("strike", "")),
            str(raw.get("right", "")),
        ))

    def _validate_existing(
        self,
        row: Instrument,
        instrument_id: UUID,
        symbol: str,
        underlying_symbol: str,
        key: tuple[date, Decimal, OptionRight],
    ) -> None:
        expiration, strike, right = key
        expected = {
            "instrument_id": instrument_id,
            "symbol": symbol,
            "asset_class": "OPTION",
            "currency": "USD",
            "underlying_symbol": underlying_symbol,
            "contract_symbol": symbol,
            "expiration_date": expiration,
            "strike_price": strike,
            "option_right": right.value,
            "contract_multiplier": STANDARD_MULTIPLIER,
            "listing_type": "STANDARD",
            "retired_at": None,
        }
        actual = {name: getattr(row, name) for name in expected}
        if actual != expected:
            self._conflict("existing instrument differs from canonical provider identity")

    @staticmethod
    def _conflict(diagnostic: str) -> None:
        raise OptionEnrollmentFailure(
            OptionEnrollmentReasonCode.CANONICAL_ATTRIBUTE_CONFLICT,
            diagnostic,
        )
