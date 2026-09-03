import hashlib
import importlib.util
import inspect
import json
import struct
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from engine.data.theta_v3 import (
    DecodedThetaSection,
    ThetaDecodedArtifactReader,
    ThetaDecodedArtifactSerializer,
)
from engine.data.provider_adapter import ProviderArtifactType
from engine.validation.feed_loader import DataNormalizer, canonical_json_bytes
from engine.validation.models import CanonicalMarketBar, CanonicalOptionChainSnapshot

SCRIPT_PATH = Path(__file__).resolve().parents[4] / "scripts" / "data" / "fetch_pilot_corpus.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("fetch_pilot_corpus", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
fetch_pilot_corpus = importlib.util.module_from_spec(SCRIPT_SPEC)
SCRIPT_SPEC.loader.exec_module(fetch_pilot_corpus)


def _artifact() -> bytes:
    return ThetaDecodedArtifactSerializer().serialize(
        [DecodedThetaSection(
            "stock_history_ohlc",
            {"symbol": "TQQQ"},
            [{
                "timestamp": datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
                "open": Decimal("10.10"), "high": Decimal("10.30"),
                "low": Decimal("10.00"), "close": Decimal("10.20"), "volume": 42,
            }],
        )],
        acquisition_request={"symbol": "TQQQ"},
    )


def _rewrite_header(content: bytes, **updates: str) -> bytes:
    offset = len(ThetaDecodedArtifactSerializer.MAGIC)
    frames = []
    while offset < len(content):
        size = struct.unpack(">Q", content[offset:offset + 8])[0]
        offset += 8
        frames.append(json.loads(content[offset:offset + size]))
        offset += size
    frames[0].update(updates)
    encoded = [canonical_json_bytes(frame) for frame in frames]
    return ThetaDecodedArtifactSerializer.MAGIC + b"".join(
        struct.pack(">Q", len(frame)) + frame for frame in encoded
    )


def test_decoded_reader_round_trip_preserves_authoritative_hash_and_typed_rows():
    content = _artifact()
    digest = hashlib.sha256(content).hexdigest()
    decoded = ThetaDecodedArtifactReader().read_provider_artifact(SimpleNamespace(
        artifact_type=ProviderArtifactType.DECODED_PROVIDER_ARTIFACT,
        format_version="THETA_PROTOBUF_DECODED-v1",
        serializer_version="KAIRO-THETA-DECODED-SERIALIZER-v1",
        content=content,
        content_sha256=digest,
    ))

    assert decoded.content_sha256 == digest
    assert decoded.format_version == "THETA_PROTOBUF_DECODED-v1"
    assert decoded.serializer_version == "KAIRO-THETA-DECODED-SERIALIZER-v1"
    assert decoded.sections[0].records[0]["close"] == Decimal("10.20")


def test_decoded_reader_fails_closed_on_content_sha256_mismatch():
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        ThetaDecodedArtifactReader().read(_artifact(), expected_content_sha256="0" * 64)


def test_decoded_reader_fails_closed_on_framing_mismatch():
    content = b"X" + _artifact()[1:]
    with pytest.raises(ValueError, match="framing mismatch"):
        ThetaDecodedArtifactReader().read(
            content, expected_content_sha256=hashlib.sha256(content).hexdigest()
        )


def test_decoded_reader_fails_closed_on_format_version_drift():
    content = _rewrite_header(_artifact(), format_version="THETA_PROTOBUF_DECODED-v2")
    with pytest.raises(ValueError, match="version drift"):
        ThetaDecodedArtifactReader().read(
            content, expected_content_sha256=hashlib.sha256(content).hexdigest()
        )


def test_decoded_reader_fails_closed_on_serializer_version_drift():
    content = _rewrite_header(_artifact(), serializer_version="KAIRO-THETA-DECODED-SERIALIZER-v2")
    with pytest.raises(ValueError, match="version drift"):
        ThetaDecodedArtifactReader().read(
            content, expected_content_sha256=hashlib.sha256(content).hexdigest()
        )


def test_theta_decoded_rows_enter_typed_bar_normalization_without_text_serialization():
    instrument_id = uuid4()
    session = SimpleNamespace(get=lambda model, key: SimpleNamespace(
        instrument_id=instrument_id, symbol="TQQQ", asset_class="ETF", retired_at=None
    ))
    content = _artifact()
    decoded = ThetaDecodedArtifactReader().read(
        content, expected_content_sha256=hashlib.sha256(content).hexdigest()
    )

    bars = DataNormalizer(session).normalize_theta_bars(
        decoded.sections, instrument_id=instrument_id, symbol="TQQQ"
    )

    assert bars[0].completed_at == datetime(2024, 1, 2, 14, 31, tzinfo=timezone.utc)
    assert bars[0].volume == Decimal("42")


def test_typed_option_chain_entry_point_reuses_existing_canonical_domain_model():
    instrument_id = uuid4()
    session = SimpleNamespace(get=lambda model, key: SimpleNamespace(
        instrument_id=instrument_id, symbol="TQQQ", asset_class="ETF", retired_at=None
    ))
    snapshot = CanonicalOptionChainSnapshot(
        underlying_instrument_id=instrument_id,
        underlying_symbol="TQQQ",
        canonical_completed_at=datetime(2024, 1, 2, 15, 0, tzinfo=timezone.utc),
        contracts=(),
    )

    assert DataNormalizer(session).normalize_typed_option_chains((snapshot,)) == (snapshot,)


def test_pilot_derives_decisions_from_completed_bars_and_has_no_signals_file_path():
    instrument_id = uuid4()
    start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    closes = [Decimal("10")] * 9 + [Decimal("9")]
    bars = tuple(CanonicalMarketBar(
        instrument_id=instrument_id, symbol="TQQQ",
        interval_start_at=start + timedelta(minutes=index),
        completed_at=start + timedelta(minutes=index + 1),
        open=close, high=close, low=close, close=close, volume=None,
    ) for index, close in enumerate(closes))

    decisions = fetch_pilot_corpus.derive_strategy_001_decisions(bars)

    assert len(decisions) == 1
    assert decisions[0].signal_at == bars[-1].completed_at
    assert decisions[0].underlying_spot == Decimal("9")
    assert "signals.json" not in inspect.getsource(fetch_pilot_corpus)
