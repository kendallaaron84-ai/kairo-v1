from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.domain.enums import OptionRight
from app.domain.instruments import CanonicalInstrument
from engine.strategy.option_resolver import (
    LegacySessionExpirationResolver,
    MappingInstrumentLookup,
    OptionContractCandidate,
    is_legacy_eligible,
    resolve_legacy_option,
)


SESSION_DATE = date(2026, 8, 28)


def contract(**updates) -> OptionContractCandidate:
    values = {
        "instrument_id": uuid4(),
        "underlying_symbol": "TQQQ",
        "expiration_date": SESSION_DATE,
        "strike_price": Decimal("50"),
        "option_right": OptionRight.CALL,
        "contract_symbol": "TQQQ260828C00050000",
        "contract_multiplier": Decimal("100"),
        "listing_type": "STANDARD",
        "bid": Decimal("0.47"),
        "ask": Decimal("0.50"),
        "volume": 10,
        "open_interest": 0,
    }
    values.update(updates)
    return OptionContractCandidate(**values)


def canonical(candidate: OptionContractCandidate, **updates) -> CanonicalInstrument:
    values = {
        "instrument_id": candidate.instrument_id,
        "symbol": f"OPT-{candidate.instrument_id.hex[:12]}",
        "asset_class": "OPTION",
        "underlying_symbol": candidate.underlying_symbol,
        "contract_symbol": candidate.contract_symbol,
        "expiration_date": candidate.expiration_date,
        "strike_price": candidate.strike_price,
        "option_right": candidate.option_right,
        "contract_multiplier": candidate.contract_multiplier,
        "listing_type": candidate.listing_type,
    }
    values.update(updates)
    return CanonicalInstrument(**values)


def resolve(
    candidates: tuple[OptionContractCandidate, ...],
    canonical_instruments: tuple[CanonicalInstrument, ...] | None = None,
):
    instruments = canonical_instruments or tuple(canonical(item) for item in candidates)
    return resolve_legacy_option(
        candidates=candidates,
        underlying_symbol="TQQQ",
        expiration_date=SESSION_DATE,
        option_right=OptionRight.CALL,
        spot_price=Decimal("50"),
        canonical_lookup=MappingInstrumentLookup(instruments),
    )


def test_option_filter_requires_bid_and_ask_positive() -> None:
    assert is_legacy_eligible(contract(bid=Decimal("0"))) is False
    assert is_legacy_eligible(contract(ask=Decimal("0"))) is False


def test_option_filter_applies_premium_cap() -> None:
    assert is_legacy_eligible(contract(ask=Decimal("0.51"), bid=Decimal("0.49"))) is False
    assert is_legacy_eligible(contract(ask=Decimal("0.50"))) is True


def test_option_filter_applies_spread_cap() -> None:
    assert is_legacy_eligible(contract(bid=Decimal("0.46"), ask=Decimal("0.50"))) is False
    assert is_legacy_eligible(contract(bid=Decimal("0.47"), ask=Decimal("0.50"))) is True


def test_option_filter_accepts_volume_threshold_or_oi_threshold() -> None:
    volume_only = contract(volume=10, open_interest=0)
    open_interest_only = contract(volume=0, open_interest=50)
    neither = contract(volume=9, open_interest=49)
    assert is_legacy_eligible(volume_only) is True
    assert is_legacy_eligible(open_interest_only) is True
    assert is_legacy_eligible(neither) is False


def test_option_resolver_filters_before_nearest_strike_selection() -> None:
    closest_but_ineligible = contract(
        strike_price=Decimal("50"),
        ask=Decimal("0.60"),
        contract_symbol="TQQQ260828C00050000",
    )
    farther_but_eligible = contract(
        strike_price=Decimal("51"),
        contract_symbol="TQQQ260828C00051000",
    )
    resolved = resolve((closest_but_ineligible, farther_but_eligible))
    assert resolved is not None
    assert resolved.strike_price == Decimal("51")
    assert resolved.contract_symbol == "TQQQ260828C00051000"


def test_legacy_expiration_resolved_once_per_session() -> None:
    resolver = LegacySessionExpirationResolver(session_date=SESSION_DATE)
    first = resolver.resolve(
        "TQQQ", (date(2026, 9, 11), date(2026, 9, 4))
    )
    second = resolver.resolve("TQQQ", (SESSION_DATE,))
    same_day = LegacySessionExpirationResolver(session_date=SESSION_DATE).resolve(
        "SQQQ", (date(2026, 9, 4), SESSION_DATE)
    )
    assert first == date(2026, 9, 4)
    assert second == date(2026, 9, 4)
    assert same_day == SESSION_DATE


def test_option_contract_uses_canonical_multiplier() -> None:
    candidate = contract(contract_multiplier=Decimal("10"))
    resolved = resolve((candidate,), (canonical(candidate),))
    assert resolved is not None
    assert resolved.contract_multiplier == Decimal("10")


def test_option_resolver_uses_canonical_multiplier() -> None:
    candidate = contract(contract_multiplier=Decimal("10"))
    authoritative = canonical(candidate, contract_multiplier=Decimal("10"))
    resolved = resolve((candidate,), (authoritative,))
    assert resolved is not None
    assert resolved.contract_multiplier == authoritative.contract_multiplier


def test_option_resolver_rejects_candidate_multiplier_mismatch() -> None:
    candidate = contract(contract_multiplier=Decimal("100"))
    authoritative = canonical(candidate, contract_multiplier=Decimal("10"))
    with pytest.raises(ValueError, match="contract_multiplier"):
        resolve((candidate,), (authoritative,))


def test_option_resolver_returns_canonical_instrument_id() -> None:
    canonical_id = UUID("22222222-2222-4222-8222-222222222222")
    candidate = contract(instrument_id=canonical_id)
    resolved = resolve((candidate,), (canonical(candidate),))
    assert resolved is not None
    assert resolved.instrument_id == canonical_id
