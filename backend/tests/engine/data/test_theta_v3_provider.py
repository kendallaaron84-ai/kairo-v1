import hashlib
import json
import struct
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from engine.data.provider_adapter import (
    ProviderArtifactType,
    ProviderTransportPayload,
    ThetaDataProviderAdapter,
)
from engine.data.theta_v3 import (
    DecodedThetaSection,
    ThetaDataV3ClientTransport,
    ThetaDecodedArtifactSerializer,
)


class RowFrame:
    def __init__(self, rows):
        self._rows = rows

    def to_dicts(self):
        return list(self._rows)


def decode_frames(content: bytes) -> list[dict]:
    offset = len(ThetaDecodedArtifactSerializer.MAGIC)
    result = []
    while offset < len(content):
        length = struct.unpack(">Q", content[offset : offset + 8])[0]
        offset += 8
        result.append(json.loads(content[offset : offset + length]))
        offset += length
    return result


def test_decoded_artifact_is_byte_deterministic_with_typed_values_and_canonical_order():
    serializer = ThetaDecodedArtifactSerializer()
    first = RowFrame(
        [
            {
                "price": 0.1,
                "timestamp": datetime(2024, 1, 2, 10, 30, tzinfo=timezone.utc),
                "size": 2,
                "missing": float("nan"),
                "strike": Decimal("100.2500"),
            },
            {
                "price": 0.2,
                "timestamp": datetime(2024, 1, 2, 10, 29, tzinfo=timezone.utc),
                "size": 1,
                "missing": None,
                "strike": Decimal("99.00"),
            },
        ]
    )
    second = RowFrame(list(reversed(first.to_dicts())))
    kwargs = {"symbol": "TQQQ", "interval": "1m"}

    one = serializer.serialize([DecodedThetaSection("stock_history_ohlc", kwargs, first)], acquisition_request=kwargs)
    two = serializer.serialize([DecodedThetaSection("stock_history_ohlc", kwargs, second)], acquisition_request=dict(reversed(list(kwargs.items()))))

    assert one == two
    assert hashlib.sha256(one).hexdigest() == hashlib.sha256(two).hexdigest()
    header, section = decode_frames(one)
    assert header["artifact_type"] == "DECODED_PROVIDER_ARTIFACT"
    assert header["format_version"] == "THETA_PROTOBUF_DECODED-v1"
    assert header["source_wire_sha256s"] == []
    assert section["fields"] == ["missing", "price", "size", "strike", "timestamp"]
    flattened = [cell for row in section["rows"] for cell in row]
    assert {cell["type"] for cell in flattened} >= {"null", "float64", "integer", "decimal", "timestamp"}
    assert any(cell.get("value") == "0x1.999999999999ap-4" for cell in flattened)
    assert any(cell.get("value") == "2024-01-02T10:30:00.000000Z" for cell in flattened)


class FakeThetaClient:
    def __init__(self):
        self.calls = []

    def _record(self, endpoint, kwargs, rows):
        self.calls.append((endpoint, kwargs))
        return RowFrame(rows)

    def stock_history_ohlc(self, **kwargs):
        return self._record("stock_history_ohlc", kwargs, [{"timestamp": datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc), "open": 1.0}])

    def option_list_contracts(self, **kwargs):
        return self._record("option_list_contracts", kwargs, [{"expiration": "2024-01-02"}, {"expiration": "2024-01-09"}, {"expiration": "2024-02-01"}])

    def option_history_quote(self, **kwargs):
        return self._record("option_history_quote", kwargs, [{"bid": 1.0, "ask": 1.1, "bid_size": 2, "ask_size": 3}])

    def option_history_open_interest(self, **kwargs):
        return self._record("option_history_open_interest", kwargs, [{"open_interest": None}])

    def option_history_ohlc(self, **kwargs):
        return self._record("option_history_ohlc", kwargs, [{"volume": 4}])

    def option_history_trade(self, **kwargs):
        return self._record("option_history_trade", kwargs, [{"size": 4}])


def test_official_client_transport_is_fail_closed_before_paid_history():
    client = FakeThetaClient()
    adapter = ThetaDataProviderAdapter(ThetaDataV3ClientTransport(client))
    with pytest.raises(RuntimeError, match="paid Theta historical retrieval is disabled"):
        adapter.fetch_equity_minute_bars(
            symbol="TQQQ", start_session=date(2024, 1, 2), end_session=date(2024, 1, 3)
        )
    assert client.calls == []


def test_official_client_equity_mapping_produces_typed_decoded_provider_artifact():
    client = FakeThetaClient()
    adapter = ThetaDataProviderAdapter(
        ThetaDataV3ClientTransport(client, allow_paid_historical_retrieval=True)
    )
    artifact = adapter.fetch_equity_minute_bars(
        symbol="tqqq", start_session=date(2024, 1, 2), end_session=date(2024, 1, 3)
    )

    assert client.calls[0][0] == "stock_history_ohlc"
    assert client.calls[0][1]["interval"] == "1m"
    assert client.calls[0][1]["venue"] == "utp_cta"
    assert artifact.artifact_type is ProviderArtifactType.DECODED_PROVIDER_ARTIFACT
    assert artifact.format_version == "THETA_PROTOBUF_DECODED-v1"
    assert artifact.serializer_version == "KAIRO-THETA-DECODED-SERIALIZER-v1"
    assert artifact.source_wire_sha256 is None
    assert artifact.content.startswith(ThetaDecodedArtifactSerializer.MAGIC)
    assert artifact.registry_raw_fields() == {
        "raw_bytes": artifact.content,
        "raw_mime_type": "application/vnd.kairo.theta-protobuf-decoded",
    }


def test_equity_history_is_partitioned_at_calendar_month_boundaries():
    client = FakeThetaClient()
    adapter = ThetaDataProviderAdapter(
        ThetaDataV3ClientTransport(client, allow_paid_historical_retrieval=True)
    )
    adapter.fetch_equity_minute_bars(
        symbol="TQQQ", start_session=date(2024, 1, 15), end_session=date(2024, 3, 2)
    )
    assert [
        (kwargs["start_date"], kwargs["end_date"])
        for endpoint, kwargs in client.calls
        if endpoint == "stock_history_ohlc"
    ] == [
        (date(2024, 1, 15), date(2024, 1, 31)),
        (date(2024, 2, 1), date(2024, 2, 29)),
        (date(2024, 3, 1), date(2024, 3, 2)),
    ]


def test_option_transport_uses_causal_end_time_previous_session_oi_and_frozen_envelope():
    client = FakeThetaClient()
    adapter = ThetaDataProviderAdapter(
        ThetaDataV3ClientTransport(client, allow_paid_historical_retrieval=True)
    )
    signal = datetime(2024, 1, 2, 15, 45, tzinfo=timezone.utc)
    artifact = adapter.fetch_option_neighborhood(symbol="sqqq", signal_at=signal)

    endpoints = [endpoint for endpoint, _ in client.calls]
    assert endpoints[0] == "option_list_contracts"
    assert client.calls[0][1] == {
        "request_type": "quote",
        "date": date(2024, 1, 2),
        "symbol": "SQQQ",
        "max_dte": 30,
    }
    quote_calls = [kwargs for endpoint, kwargs in client.calls if endpoint == "option_history_quote"]
    oi_calls = [kwargs for endpoint, kwargs in client.calls if endpoint == "option_history_open_interest"]
    trade_calls = [kwargs for endpoint, kwargs in client.calls if endpoint == "option_history_trade"]
    ohlc_calls = [kwargs for endpoint, kwargs in client.calls if endpoint == "option_history_ohlc"]
    assert {call["expiration"] for call in quote_calls} == {date(2024, 1, 2), date(2024, 1, 9), date(2024, 2, 1)}
    assert all(call["interval"] == "1m" and call["strike_range"] == 10 for call in quote_calls)
    assert all(call["end_time"].isoformat() == "10:45:00" for call in quote_calls + trade_calls)
    assert all(call["end_time"].isoformat() == "10:44:00" for call in ohlc_calls)
    assert all("end_time" not in call and call["date"] == date(2024, 1, 2) for call in oi_calls)
    assert artifact.artifact_type is ProviderArtifactType.DECODED_PROVIDER_ARTIFACT
    frames = decode_frames(artifact.content)
    assert any(
        cell == {"type": "null"}
        for section in frames[1:]
        if section["endpoint"] == "option_history_open_interest"
        for row in section["rows"]
        for cell in row
    )


def test_naked_byte_transport_is_not_misrepresented_as_wire_or_decoded_dataframe():
    artifact = ThetaDataProviderAdapter(lambda _kind, _params: b"response-data-wire").fetch_equity_minute_bars(
        symbol="TQQQ", start_session=date(2024, 1, 2), end_session=date(2024, 1, 2)
    )
    assert artifact.artifact_type is ProviderArtifactType.OPAQUE_PROVIDER_ARTIFACT
    assert artifact.format_version == "THETA_PROVIDER_BYTES_UNCLASSIFIED-v1"
    assert artifact.serializer_version is None


def test_transport_can_explicitly_identify_exact_response_data_wire_bytes():
    payload = ProviderTransportPayload(
        content=b"exact-response-data-wire",
        mime_type="application/x-protobuf",
        artifact_type=ProviderArtifactType.WIRE_PROVIDER_ARTIFACT,
        format_version="THETA_RESPONSE_DATA_WIRE-v1",
    )
    artifact = ThetaDataProviderAdapter(lambda _kind, _params: payload).fetch_equity_minute_bars(
        symbol="TQQQ", start_session=date(2024, 1, 2), end_session=date(2024, 1, 2)
    )
    assert artifact.artifact_type is ProviderArtifactType.WIRE_PROVIDER_ARTIFACT
    assert artifact.content_sha256 == hashlib.sha256(b"exact-response-data-wire").hexdigest()
