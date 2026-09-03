"""Idempotently enroll the frozen TQQQ/SQQQ canonical ETF underlyings."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid5

from sqlalchemy import create_engine, or_, select
from sqlalchemy.orm import Session


BACKEND_ROOT = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings  # noqa: E402
from app.db.models.configuration import Instrument  # noqa: E402
from app.repositories.configuration import InstrumentRepository  # noqa: E402
from engine.data.option_enrollment import KAIRO_INSTRUMENT_NAMESPACE  # noqa: E402


CANONICAL_UNDERLYING_SYMBOLS = ("TQQQ", "SQQQ")
CANONICAL_UNDERLYING_EFFECTIVE_FROM = datetime(2010, 2, 9, 9, 30, tzinfo=UTC)


class CanonicalUnderlyingConflict(RuntimeError):
    """An existing row occupies a frozen ETF UUID or symbol incorrectly."""


class SeedDisposition(StrEnum):
    CREATED = "CREATED"
    EXISTING = "EXISTING"


@dataclass(frozen=True)
class CanonicalUnderlyingSeedResult:
    symbol: str
    instrument_id: UUID
    disposition: SeedDisposition


def deterministic_etf_instrument_id(symbol: str) -> UUID:
    normalized_symbol = symbol.upper()
    if normalized_symbol not in CANONICAL_UNDERLYING_SYMBOLS:
        raise ValueError(f"unsupported canonical underlying symbol: {normalized_symbol}")
    canonical_key = f"ETF|{normalized_symbol}|USD|NASDAQ|STANDARD"
    return uuid5(KAIRO_INSTRUMENT_NAMESPACE, canonical_key)


def _canonical_attributes(symbol: str) -> dict[str, object]:
    return {
        "instrument_id": deterministic_etf_instrument_id(symbol),
        "symbol": symbol,
        "asset_class": "ETF",
        "currency": "USD",
        "exchange": "NASDAQ",
        "contract_multiplier": None,
        "effective_from": CANONICAL_UNDERLYING_EFFECTIVE_FROM,
        "retired_at": None,
    }


def _validate_existing(row: Instrument, expected: dict[str, object]) -> None:
    divergences = {
        name: {"expected": expected_value, "actual": getattr(row, name)}
        for name, expected_value in expected.items()
        if getattr(row, name) != expected_value
    }
    if divergences:
        details = ", ".join(
            f"{name}=expected {values['expected']!r}, actual {values['actual']!r}"
            for name, values in sorted(divergences.items())
        )
        raise CanonicalUnderlyingConflict(
            f"canonical identity conflict for {expected['symbol']}: {details}"
        )


def seed_canonical_underlyings(
    session: Session,
) -> tuple[CanonicalUnderlyingSeedResult, ...]:
    """Insert missing canonical ETFs and fail closed on either UUID or symbol collision."""
    repository = InstrumentRepository(session)
    results = []
    for symbol in CANONICAL_UNDERLYING_SYMBOLS:
        expected = _canonical_attributes(symbol)
        matches = session.scalars(
            select(Instrument).where(
                or_(
                    Instrument.instrument_id == expected["instrument_id"],
                    Instrument.symbol == symbol,
                )
            )
        ).all()
        if len(matches) > 1:
            raise CanonicalUnderlyingConflict(
                f"canonical identity conflict for {symbol}: UUID and symbol occupy different rows"
            )
        if matches:
            _validate_existing(matches[0], expected)
            disposition = SeedDisposition.EXISTING
        else:
            repository.add(Instrument(**expected))
            disposition = SeedDisposition.CREATED
        results.append(
            CanonicalUnderlyingSeedResult(
                symbol=symbol,
                instrument_id=expected["instrument_id"],
                disposition=disposition,
            )
        )
    return tuple(results)


def main() -> int:
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        with Session(engine, expire_on_commit=False) as session, session.begin():
            results = seed_canonical_underlyings(session)
        for result in results:
            print(f"{result.disposition.value}: {result.symbol} {result.instrument_id}")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
