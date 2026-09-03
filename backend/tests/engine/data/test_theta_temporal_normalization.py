import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from engine.data.theta_v3 import (
    DecodedThetaSection,
    ThetaDecodedArtifactSerializer,
)
from engine.validation.feed_loader import DataNormalizer


START = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)


def _row(minute: int) -> dict[str, object]:
    price = Decimal("10") + Decimal(minute) / Decimal("100")
    return {
        "timestamp": START + timedelta(minutes=minute),
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "volume": 100 + minute,
    }


def _section(*rows: dict[str, object], chunk: str = "a") -> SimpleNamespace:
    return SimpleNamespace(
        endpoint="stock_history_ohlc",
        parameters={"symbol": "TQQQ", "chunk": chunk},
        records=rows,
    )


def _normalizer():
    instrument_id = uuid4()
    session = SimpleNamespace(
        get=lambda model, key: SimpleNamespace(
            instrument_id=instrument_id,
            symbol="TQQQ",
            asset_class="ETF",
            retired_at=None,
        )
    )
    return DataNormalizer(session), instrument_id


def test_normalize_theta_bars_orders_non_chronological_decoded_rows_into_strict_timestamp_sequence():
    normalizer, instrument_id = _normalizer()

    bars = normalizer.normalize_theta_bars(
        (_section(_row(2), _row(0), _row(1)),),
        instrument_id=instrument_id,
        symbol="TQQQ",
    )

    assert [bar.completed_at for bar in bars] == [
        START + timedelta(minutes=1),
        START + timedelta(minutes=2),
        START + timedelta(minutes=3),
    ]


def test_normalize_theta_bars_is_invariant_to_shuffled_or_reversed_section_input_order():
    normalizer, instrument_id = _normalizer()
    forward = (
        _section(_row(1), _row(0), chunk="a"),
        _section(_row(3), _row(2), chunk="b"),
    )
    reversed_sections = tuple(reversed(forward))

    first = normalizer.normalize_theta_bars(
        forward, instrument_id=instrument_id, symbol="TQQQ"
    )
    second = normalizer.normalize_theta_bars(
        reversed_sections, instrument_id=instrument_id, symbol="TQQQ"
    )

    assert first == second


def test_normalize_theta_bars_fails_closed_on_duplicate_bar_timestamps():
    normalizer, instrument_id = _normalizer()

    with pytest.raises(ValueError, match="duplicate timestamp for TQQQ"):
        normalizer.normalize_theta_bars(
            (_section(_row(0), chunk="a"), _section(_row(0), chunk="b")),
            instrument_id=instrument_id,
            symbol="TQQQ",
        )


def test_theta_protobuf_decoded_v1_serialization_remains_byte_deterministic():
    serializer = ThetaDecodedArtifactSerializer()
    parameters_a = {"symbol": "TQQQ", "chunk": "a"}
    parameters_b = {"symbol": "TQQQ", "chunk": "b"}
    forward = [
        DecodedThetaSection("stock_history_ohlc", parameters_a, [_row(1), _row(0)]),
        DecodedThetaSection("stock_history_ohlc", parameters_b, [_row(3), _row(2)]),
    ]
    shuffled = [
        DecodedThetaSection("stock_history_ohlc", dict(reversed(list(parameters_b.items()))), [_row(2), _row(3)]),
        DecodedThetaSection("stock_history_ohlc", dict(reversed(list(parameters_a.items()))), [_row(0), _row(1)]),
    ]

    first = serializer.serialize(
        forward, acquisition_request={"symbol": "TQQQ", "interval": "1m"}
    )
    second = serializer.serialize(
        shuffled, acquisition_request={"interval": "1m", "symbol": "TQQQ"}
    )

    assert first == second
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()
    assert first.startswith(ThetaDecodedArtifactSerializer.MAGIC)
