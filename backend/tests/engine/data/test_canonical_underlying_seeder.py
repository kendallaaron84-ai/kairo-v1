from datetime import UTC, datetime
from pathlib import Path
import sys
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.db.models.configuration import Instrument


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.data.seed_canonical_underlyings import (  # noqa: E402
    CANONICAL_UNDERLYING_EFFECTIVE_FROM,
    CanonicalUnderlyingConflict,
    SeedDisposition,
    deterministic_etf_instrument_id,
    seed_canonical_underlyings,
)


def test_canonical_underlying_uuid5_ids_are_deterministic() -> None:
    assert deterministic_etf_instrument_id("TQQQ") == deterministic_etf_instrument_id(
        "tqqq"
    )
    assert deterministic_etf_instrument_id("SQQQ") != deterministic_etf_instrument_id(
        "TQQQ"
    )


def test_canonical_underlying_seed_is_idempotent(db_session: Session) -> None:
    created = seed_canonical_underlyings(db_session)
    existing = seed_canonical_underlyings(db_session)

    assert [result.disposition for result in created] == [
        SeedDisposition.CREATED,
        SeedDisposition.CREATED,
    ]
    assert [result.disposition for result in existing] == [
        SeedDisposition.EXISTING,
        SeedDisposition.EXISTING,
    ]
    for result in existing:
        row = db_session.get(Instrument, result.instrument_id)
        assert row is not None
        assert row.symbol == result.symbol
        assert row.asset_class == "ETF"
        assert row.currency == "USD"
        assert row.exchange == "NASDAQ"
        assert row.effective_from == CANONICAL_UNDERLYING_EFFECTIVE_FROM
        assert row.contract_multiplier is None
        assert row.retired_at is None


def test_canonical_underlying_seed_rejects_symbol_identity_collision(
    db_session: Session,
) -> None:
    db_session.add(
        Instrument(
            instrument_id=uuid4(),
            symbol="TQQQ",
            asset_class="ETF",
            currency="USD",
            exchange="NASDAQ",
            effective_from=datetime(2010, 2, 9, 9, 30, tzinfo=UTC),
        )
    )
    db_session.flush()

    with pytest.raises(CanonicalUnderlyingConflict, match="instrument_id"):
        seed_canonical_underlyings(db_session)


def test_canonical_underlying_seed_rejects_attribute_drift(
    db_session: Session,
) -> None:
    db_session.add(
        Instrument(
            instrument_id=deterministic_etf_instrument_id("TQQQ"),
            symbol="TQQQ",
            asset_class="ETF",
            currency="USD",
            exchange="NYSE",
            effective_from=CANONICAL_UNDERLYING_EFFECTIVE_FROM,
        )
    )
    db_session.flush()

    with pytest.raises(CanonicalUnderlyingConflict, match="exchange"):
        seed_canonical_underlyings(db_session)
