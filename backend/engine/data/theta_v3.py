"""Official Theta Data v3 client transport and deterministic provider artifacts.

No call is made merely by importing this module. Historical methods are guarded
by an explicit paid-retrieval authorization supplied by the acquisition caller.
"""

from __future__ import annotations

import base64
import hashlib
import math
import numbers
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from engine.validation.feed_loader import canonical_json_bytes

from .provider_adapter import ProviderArtifactType, ProviderTransportPayload


class ThetaDataFrame(Protocol):
    columns: Any


class ThetaV3Client(Protocol):
    def stock_history_ohlc(self, **kwargs: Any) -> ThetaDataFrame: ...
    def option_list_contracts(self, **kwargs: Any) -> ThetaDataFrame: ...
    def option_history_quote(self, **kwargs: Any) -> ThetaDataFrame: ...
    def option_history_open_interest(self, **kwargs: Any) -> ThetaDataFrame: ...
    def option_history_ohlc(self, **kwargs: Any) -> ThetaDataFrame: ...
    def option_history_trade(self, **kwargs: Any) -> ThetaDataFrame: ...


@dataclass(frozen=True)
class DecodedThetaSection:
    endpoint: str
    parameters: Mapping[str, Any]
    dataframe: Any


class ThetaDecodedArtifactSerializer:
    """Canonical framing for SDK-decoded Theta DataFrames.

    Values are typed before encoding. Floats use their exact IEEE-754 hexadecimal
    form, Decimals retain sign/digits/exponent, timestamps are UTC, nulls are an
    explicit tagged value, fields are lexical, and rows are sorted by encoded
    content. This is intentionally not described as a Theta wire payload.
    """

    FORMAT_VERSION = "THETA_PROTOBUF_DECODED-v1"
    SERIALIZER_VERSION = "KAIRO-THETA-DECODED-SERIALIZER-v1"
    MIME_TYPE = "application/vnd.kairo.theta-protobuf-decoded"
    MAGIC = b"KAIRO-THETA-DECODED\x00\x01"

    def serialize(
        self,
        sections: Sequence[DecodedThetaSection],
        *,
        acquisition_request: Mapping[str, Any],
        source_wire_sha256s: Sequence[str] = (),
    ) -> bytes:
        wire_hashes = sorted(set(source_wire_sha256s))
        if any(
            len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest)
            for digest in wire_hashes
        ):
            raise ValueError("source wire SHA-256 values must be lowercase hexadecimal digests")
        encoded_sections = [self._section(section) for section in sections]
        encoded_sections.sort(key=canonical_json_bytes)
        header = {
            "artifact_type": ProviderArtifactType.DECODED_PROVIDER_ARTIFACT,
            "format_version": self.FORMAT_VERSION,
            "serializer_version": self.SERIALIZER_VERSION,
            "field_order": "UNICODE_CODEPOINT_ASCENDING",
            "row_order": "LEXICOGRAPHIC_CANONICAL_TYPED_ROW_BYTES",
            "timestamp_encoding": "UTC_RFC3339_MICROSECONDS_Z",
            "null_encoding": {"type": "null"},
            "integer_encoding": "BASE10_STRING",
            "float_encoding": "PYTHON_FLOAT_HEX_IEEE754_BINARY64",
            "decimal_encoding": "SIGN_DIGITS_EXPONENT",
            "framing": "MAGIC_THEN_UINT64_BE_LENGTH_PREFIXED_CANONICAL_JSON_FRAMES",
            "acquisition_request": self._canonical_mapping(acquisition_request),
            "source_wire_sha256s": wire_hashes,
            "section_count": len(encoded_sections),
        }
        frames = [
            canonical_json_bytes(header),
            *(canonical_json_bytes(section) for section in encoded_sections),
        ]
        return self.MAGIC + b"".join(struct.pack(">Q", len(frame)) + frame for frame in frames)

    def _section(self, section: DecodedThetaSection) -> dict[str, Any]:
        records = self._records(section.dataframe)
        fields = sorted({str(key) for row in records for key in row})
        rows = [
            [self._typed(row.get(field)) if field in row else {"type": "null"} for field in fields]
            for row in records
        ]
        rows.sort(key=canonical_json_bytes)
        return {
            "endpoint": section.endpoint,
            "parameters": self._canonical_mapping(section.parameters),
            "fields": fields,
            "rows": rows,
        }

    @staticmethod
    def _records(dataframe: Any) -> list[dict[str, Any]]:
        if hasattr(dataframe, "to_dicts"):
            rows = dataframe.to_dicts()
        elif hasattr(dataframe, "to_dict"):
            try:
                rows = dataframe.to_dict(orient="records")
            except TypeError:
                mapping = dataframe.to_dict()
                keys = list(mapping)
                row_indexes = sorted({index for column in mapping.values() for index in column})
                rows = [{key: mapping[key].get(index) for key in keys} for index in row_indexes]
        elif isinstance(dataframe, Sequence) and not isinstance(dataframe, (str, bytes, bytearray)):
            rows = list(dataframe)
        else:
            raise TypeError(
                "Theta decoded response must be a Pandas/Polars DataFrame or row sequence"
            )
        if any(not isinstance(row, Mapping) for row in rows):
            raise TypeError("Theta decoded response rows must be mappings")
        return [{str(key): value for key, value in row.items()} for row in rows]

    def _canonical_mapping(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return {str(key): self._typed(values[key]) for key in sorted(values, key=str)}

    def _typed(self, value: Any) -> dict[str, Any]:
        if self._is_null(value):
            return {"type": "null"}
        if isinstance(value, bool):
            return {"type": "boolean", "value": value}
        if isinstance(value, Decimal):
            parts = value.as_tuple()
            return {
                "type": "decimal",
                "sign": parts.sign,
                "digits": "".join(str(digit) for digit in parts.digits),
                "exponent": str(parts.exponent),
            }
        if isinstance(value, numbers.Integral):
            return {"type": "integer", "value": str(value)}
        if isinstance(value, numbers.Real):
            return {"type": "float64", "value": float(value).hex()}
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("decoded Theta timestamps must be timezone-aware")
            rendered = value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            return {"type": "timestamp", "value": rendered}
        if isinstance(value, date):
            return {"type": "date", "value": value.isoformat()}
        if isinstance(value, time):
            if value.tzinfo is not None:
                raise ValueError("Theta request wall-clock times must not carry a timezone")
            return {"type": "wall_clock_time", "value": value.isoformat(timespec="microseconds")}
        if isinstance(value, bytes):
            return {"type": "bytes", "value": base64.b64encode(value).decode("ascii")}
        if isinstance(value, str):
            return {"type": "text", "value": value}
        raise TypeError(f"unsupported decoded Theta value type: {type(value).__name__}")

    @staticmethod
    def _is_null(value: Any) -> bool:
        if value is None or type(value).__name__ in {"NAType", "NaTType"}:
            return True
        return (
            isinstance(value, numbers.Real)
            and not isinstance(value, numbers.Integral)
            and math.isnan(float(value))
        )


class ThetaDataV3ClientTransport:
    """Callable transport for ``ThetaDataProviderAdapter`` using the official SDK.

    The SDK's public methods return only decoded DataFrames in v1.0.10. The
    transport therefore emits ``DECODED_PROVIDER_ARTIFACT`` bytes and never
    labels a DataFrame serialization as original protobuf/gRPC wire bytes.
    """

    CONTRACT_REQUEST_TYPE = "quote"
    RTH_START = time(9, 30)
    RTH_END = time(16, 0)

    def __init__(
        self,
        client: ThetaV3Client,
        *,
        allow_paid_historical_retrieval: bool = False,
        serializer: ThetaDecodedArtifactSerializer | None = None,
    ) -> None:
        self._client = client
        self._allow_paid = allow_paid_historical_retrieval
        self._serializer = serializer or ThetaDecodedArtifactSerializer()

    @classmethod
    def from_local_environment(
        cls,
        *,
        allow_paid_historical_retrieval: bool = False,
        dotenv_path: str | Path | None = None,
    ) -> "ThetaDataV3ClientTransport":
        from thetadata import ThetaClient

        resolved_dotenv = dotenv_path or Path(__file__).resolve().parents[2] / ".env"
        client = ThetaClient(dataframe_type="polars", dotenv_path=resolved_dotenv)
        return cls(client, allow_paid_historical_retrieval=allow_paid_historical_retrieval)

    def __call__(
        self, request_kind: str, parameters: dict[str, Any]
    ) -> ProviderTransportPayload:
        if not self._allow_paid:
            raise RuntimeError(
                "paid Theta historical retrieval is disabled; "
                "explicit acquisition authorization is required"
            )
        if request_kind == "EQUITY_RTH_1MIN_BARS":
            sections = self._equity_sections(parameters)
        elif request_kind == "OPTION_STRIKE_NEIGHBORHOOD":
            sections = self._option_sections(parameters)
        else:
            raise ValueError(f"unsupported Theta request kind: {request_kind}")
        content = self._serializer.serialize(sections, acquisition_request=parameters)
        return ProviderTransportPayload(
            content=content,
            mime_type=self._serializer.MIME_TYPE,
            artifact_type=ProviderArtifactType.DECODED_PROVIDER_ARTIFACT,
            format_version=self._serializer.FORMAT_VERSION,
            serializer_version=self._serializer.SERIALIZER_VERSION,
        )

    def _equity_sections(self, parameters: Mapping[str, Any]) -> list[DecodedThetaSection]:
        start = date.fromisoformat(str(parameters["start_session"]))
        end = date.fromisoformat(str(parameters["end_session"]))
        sections = []
        for chunk_start, chunk_end in self._calendar_month_chunks(start, end):
            kwargs = {
                "symbol": str(parameters["symbol"]),
                "interval": "1m",
                "start_date": chunk_start,
                "end_date": chunk_end,
                "start_time": self.RTH_START,
                "end_time": self.RTH_END,
                "venue": "utp_cta",
            }
            frame = self._client.stock_history_ohlc(**kwargs)
            sections.append(DecodedThetaSection("stock_history_ohlc", kwargs, frame))
        return sections

    @staticmethod
    def _calendar_month_chunks(start: date, end: date) -> tuple[tuple[date, date], ...]:
        result = []
        current = start
        while current <= end:
            next_month = (
                date(current.year + 1, 1, 1)
                if current.month == 12
                else date(current.year, current.month + 1, 1)
            )
            chunk_end = min(end, next_month - timedelta(days=1))
            result.append((current, chunk_end))
            current = chunk_end + timedelta(days=1)
        return tuple(result)

    def _option_sections(self, parameters: Mapping[str, Any]) -> list[DecodedThetaSection]:
        symbol = str(parameters["symbol"])
        target_dtes = tuple(int(value) for value in str(parameters["target_dtes"]).split(","))
        signals = tuple(
            datetime.fromisoformat(value)
            for value in str(parameters["signal_times"]).split(",")
        )
        eastern = ZoneInfo("America/New_York")
        sections: list[DecodedThetaSection] = []
        for signal in signals:
            local_signal = signal.astimezone(eastern)
            session = local_signal.date()
            list_kwargs = {
                "request_type": self.CONTRACT_REQUEST_TYPE,
                "date": session,
                "symbol": symbol,
                "max_dte": max(target_dtes),
            }
            contracts = self._client.option_list_contracts(**list_kwargs)
            sections.append(DecodedThetaSection("option_list_contracts", list_kwargs, contracts))
            expirations = self._target_expirations(contracts, session, target_dtes)
            for expiration in expirations:
                common = {
                    "symbol": symbol,
                    "expiration": expiration,
                    "date": session,
                    "strike": "*",
                    "right": "both",
                    "strike_range": int(parameters["strikes_each_side"]),
                }
                quote_kwargs = {
                    **common,
                    "interval": "1m",
                    "start_time": self.RTH_START,
                    "end_time": local_signal.time().replace(tzinfo=None),
                }
                sections.append(
                    DecodedThetaSection(
                        "option_history_quote",
                        quote_kwargs,
                        self._client.option_history_quote(**quote_kwargs),
                    )
                )
                sections.append(
                    DecodedThetaSection(
                        "option_history_open_interest",
                        common,
                        self._client.option_history_open_interest(**common),
                    )
                )
                history_kwargs = {
                    **common,
                    "start_time": self.RTH_START,
                    "end_time": local_signal.time().replace(tzinfo=None),
                }
                last_complete_open = (
                    (local_signal - timedelta(minutes=1)).time().replace(tzinfo=None)
                )
                ohlc_kwargs = {
                    **history_kwargs,
                    "end_time": last_complete_open,
                    "interval": "1m",
                }
                sections.append(
                    DecodedThetaSection(
                        "option_history_ohlc",
                        ohlc_kwargs,
                        self._client.option_history_ohlc(**ohlc_kwargs),
                    )
                )
                sections.append(
                    DecodedThetaSection(
                        "option_history_trade",
                        history_kwargs,
                        self._client.option_history_trade(**history_kwargs),
                    )
                )
        return sections

    @staticmethod
    def _target_expirations(
        frame: Any, session: date, target_dtes: tuple[int, ...]
    ) -> tuple[date, ...]:
        records = ThetaDecodedArtifactSerializer._records(frame)
        aliases = ("expiration", "expiration_date", "Expiration", "expirationDate")
        available: set[date] = set()
        for row in records:
            raw = next((row[key] for key in aliases if key in row), None)
            if raw is None:
                continue
            candidate = raw.date() if isinstance(raw, datetime) else raw
            if isinstance(candidate, str):
                candidate = date.fromisoformat(candidate[:10])
            if isinstance(candidate, date) and candidate >= session:
                available.add(candidate)
        if not available:
            raise ValueError("Theta contract list did not expose any causal expiration values")
        chosen = {
            min(available, key=lambda expiry: (abs((expiry - session).days - dte), expiry))
            for dte in target_dtes
        }
        return tuple(sorted(chosen))


def decoded_artifact_sha256(payload: ProviderTransportPayload) -> str:
    if payload.artifact_type is not ProviderArtifactType.DECODED_PROVIDER_ARTIFACT:
        raise ValueError("payload is not a decoded provider artifact")
    return hashlib.sha256(payload.content).hexdigest()
