"""Official Theta Data v3 client transport and deterministic provider artifacts.

No call is made merely by importing this module. Historical methods are guarded
by an explicit paid-retrieval authorization supplied by the acquisition caller.
"""

from __future__ import annotations

import base64
import hashlib
import json
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

from thetadata.errors import NoDataFoundError

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

    @property
    def missing_evidence(self) -> ProviderMissingEvidence | None:
        return self.dataframe if isinstance(self.dataframe, ProviderMissingEvidence) else None


@dataclass(frozen=True)
class ProviderMissingEvidence:
    """Typed, non-fabricated record of an authoritative provider no-data response."""

    provider: str
    endpoint: str
    symbol: str
    session: date
    reason_code: str
    records_count: int
    context: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.provider != "THETA_DATA":
            raise ValueError("missing evidence provider must be THETA_DATA")
        if self.reason_code != "PROVIDER_NO_DATA" or self.records_count != 0:
            raise ValueError("missing evidence must represent PROVIDER_NO_DATA with zero records")


@dataclass(frozen=True)
class DecodedThetaRecordSection:
    endpoint: str
    parameters: Mapping[str, Any]
    fields: tuple[str, ...]
    records: tuple[Mapping[str, Any], ...]
    missing_evidence: ProviderMissingEvidence | None = None


@dataclass(frozen=True)
class DecodedThetaArtifact:
    content_sha256: str
    format_version: str
    serializer_version: str
    acquisition_request: Mapping[str, Any]
    source_wire_sha256s: tuple[str, ...]
    sections: tuple[DecodedThetaRecordSection, ...]


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
    MISSING_EVIDENCE_MARKER = "__kairo_provider_missing_evidence__"

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
        if isinstance(dataframe, ProviderMissingEvidence):
            return [{
                ThetaDecodedArtifactSerializer.MISSING_EVIDENCE_MARKER: True,
                "provider": dataframe.provider,
                "endpoint": dataframe.endpoint,
                "symbol": dataframe.symbol,
                "session": dataframe.session,
                "reason_code": dataframe.reason_code,
                "records_count": dataframe.records_count,
            }]
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


class ThetaDecodedArtifactReader:
    """Strict inverse of the accepted decoded-provider serialization.

    Reading requires the authority-recorded SHA-256. Every frame must be
    canonical and every ordering/version invariant must match exactly.
    """

    def read_provider_artifact(self, artifact: Any) -> DecodedThetaArtifact:
        if artifact.artifact_type is not ProviderArtifactType.DECODED_PROVIDER_ARTIFACT:
            raise ValueError("Theta artifact is not a decoded provider artifact")
        if artifact.format_version != ThetaDecodedArtifactSerializer.FORMAT_VERSION:
            raise ValueError("Theta decoded artifact format version drift")
        if artifact.serializer_version != ThetaDecodedArtifactSerializer.SERIALIZER_VERSION:
            raise ValueError("Theta decoded artifact serializer version drift")
        return self.read(
            artifact.content,
            expected_content_sha256=artifact.content_sha256,
        )

    def read(self, content: bytes, *, expected_content_sha256: str) -> DecodedThetaArtifact:
        if len(expected_content_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in expected_content_sha256
        ):
            raise ValueError("expected content SHA-256 must be lowercase hexadecimal")
        actual_digest = hashlib.sha256(content).hexdigest()
        if actual_digest != expected_content_sha256:
            raise ValueError("Theta decoded artifact SHA-256 mismatch")
        serializer = ThetaDecodedArtifactSerializer
        if not content.startswith(serializer.MAGIC):
            raise ValueError("Theta decoded artifact framing mismatch")
        frames = self._frames(content[len(serializer.MAGIC) :])
        if not frames:
            raise ValueError("Theta decoded artifact header is absent")
        decoded_frames = []
        for frame in frames:
            try:
                decoded = json.loads(frame)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("Theta decoded artifact contains an invalid JSON frame") from error
            if canonical_json_bytes(decoded) != frame:
                raise ValueError("Theta decoded artifact frame is not canonical")
            decoded_frames.append(decoded)
        header, *section_frames = decoded_frames
        self._header(header, len(section_frames))
        if [canonical_json_bytes(item) for item in section_frames] != sorted(
            canonical_json_bytes(item) for item in section_frames
        ):
            raise ValueError("Theta decoded artifact sections are not canonically ordered")
        sections = tuple(self._section(item) for item in section_frames)
        return DecodedThetaArtifact(
            content_sha256=actual_digest,
            format_version=header["format_version"],
            serializer_version=header["serializer_version"],
            acquisition_request=self._mapping(header["acquisition_request"]),
            source_wire_sha256s=tuple(header["source_wire_sha256s"]),
            sections=sections,
        )

    @staticmethod
    def _frames(payload: bytes) -> list[bytes]:
        frames = []
        offset = 0
        while offset < len(payload):
            if len(payload) - offset < 8:
                raise ValueError("Theta decoded artifact has a truncated frame length")
            length = struct.unpack(">Q", payload[offset : offset + 8])[0]
            offset += 8
            if length == 0 or len(payload) - offset < length:
                raise ValueError("Theta decoded artifact has a truncated or empty frame")
            frames.append(payload[offset : offset + length])
            offset += length
        return frames

    @staticmethod
    def _header(header: Any, section_count: int) -> None:
        if not isinstance(header, dict):
            raise ValueError("Theta decoded artifact header must be an object")
        expected = {
            "artifact_type": ProviderArtifactType.DECODED_PROVIDER_ARTIFACT,
            "format_version": ThetaDecodedArtifactSerializer.FORMAT_VERSION,
            "serializer_version": ThetaDecodedArtifactSerializer.SERIALIZER_VERSION,
            "field_order": "UNICODE_CODEPOINT_ASCENDING",
            "row_order": "LEXICOGRAPHIC_CANONICAL_TYPED_ROW_BYTES",
            "timestamp_encoding": "UTC_RFC3339_MICROSECONDS_Z",
            "integer_encoding": "BASE10_STRING",
            "float_encoding": "PYTHON_FLOAT_HEX_IEEE754_BINARY64",
            "decimal_encoding": "SIGN_DIGITS_EXPONENT",
            "framing": "MAGIC_THEN_UINT64_BE_LENGTH_PREFIXED_CANONICAL_JSON_FRAMES",
        }
        if set(header) != set(expected) | {
            "null_encoding", "acquisition_request", "source_wire_sha256s", "section_count"
        }:
            raise ValueError("Theta decoded artifact header schema mismatch")
        if any(header.get(key) != value for key, value in expected.items()):
            raise ValueError("Theta decoded artifact schema or serializer version drift")
        if header.get("null_encoding") != {"type": "null"}:
            raise ValueError("Theta decoded artifact NULL encoding drift")
        if header.get("section_count") != section_count:
            raise ValueError("Theta decoded artifact section count mismatch")
        hashes = header.get("source_wire_sha256s")
        if (
            not isinstance(hashes, list)
            or hashes != sorted(set(hashes))
            or any(
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                for digest in hashes
            )
        ):
            raise ValueError("Theta decoded artifact source-wire lineage is invalid")
        if not isinstance(header.get("acquisition_request"), dict):
            raise ValueError("Theta decoded artifact acquisition request is invalid")

    def _section(self, value: Any) -> DecodedThetaRecordSection:
        if not isinstance(value, dict) or set(value) != {
            "endpoint",
            "parameters",
            "fields",
            "rows",
        }:
            raise ValueError("Theta decoded artifact section schema mismatch")
        if not isinstance(value["endpoint"], str):
            raise ValueError("Theta decoded artifact endpoint is invalid")
        fields = value["fields"]
        rows = value["rows"]
        if not isinstance(fields, list) or any(not isinstance(field, str) for field in fields) or fields != sorted(set(fields)):
            raise ValueError("Theta decoded artifact fields are not canonical")
        if not isinstance(rows, list) or any(
            not isinstance(row, list) or len(row) != len(fields) for row in rows
        ):
            raise ValueError("Theta decoded artifact row schema mismatch")
        if [canonical_json_bytes(row) for row in rows] != sorted(
            canonical_json_bytes(row) for row in rows
        ):
            raise ValueError("Theta decoded artifact rows are not canonically ordered")
        decoded_records = tuple(
            {field: self._value(cell) for field, cell in zip(fields, row, strict=True)}
            for row in rows
        )
        missing_evidence = self._missing_evidence(
            value["endpoint"], self._mapping(value["parameters"]), decoded_records
        )
        return DecodedThetaRecordSection(
            endpoint=str(value["endpoint"]),
            parameters=self._mapping(value["parameters"]),
            fields=tuple(fields),
            records=() if missing_evidence is not None else decoded_records,
            missing_evidence=missing_evidence,
        )

    @staticmethod
    def _missing_evidence(
        endpoint: str,
        parameters: Mapping[str, Any],
        records: tuple[Mapping[str, Any], ...],
    ) -> ProviderMissingEvidence | None:
        marker = ThetaDecodedArtifactSerializer.MISSING_EVIDENCE_MARKER
        marked = [record for record in records if record.get(marker) is True]
        if not marked:
            return None
        if len(records) != 1 or len(marked) != 1 or set(marked[0]) != {
            marker, "provider", "endpoint", "symbol", "session", "reason_code",
            "records_count",
        }:
            raise ValueError("Theta decoded artifact missing-evidence record is invalid")
        record = marked[0]
        if record["endpoint"] != endpoint:
            raise ValueError("Theta decoded artifact missing-evidence endpoint mismatch")
        return ProviderMissingEvidence(
            provider=str(record["provider"]),
            endpoint=str(record["endpoint"]),
            symbol=str(record["symbol"]),
            session=record["session"],
            reason_code=str(record["reason_code"]),
            records_count=int(record["records_count"]),
            context=parameters,
        )

    def _mapping(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or list(value) != sorted(value):
            raise ValueError("Theta decoded artifact mapping order is not canonical")
        return {str(key): self._value(item) for key, item in value.items()}

    def _value(self, value: Any) -> Any:
        if not isinstance(value, dict) or "type" not in value:
            raise ValueError("Theta decoded artifact value lacks a type tag")
        value_type = value["type"]
        if value_type == "null" and value == {"type": "null"}:
            return None
        if value_type == "boolean" and isinstance(value.get("value"), bool):
            return value["value"]
        if value_type == "integer":
            return int(value["value"])
        if value_type == "float64":
            return float.fromhex(value["value"])
        if value_type == "decimal":
            digits = tuple(int(digit) for digit in value["digits"])
            return Decimal((int(value["sign"]), digits, int(value["exponent"])))
        if value_type == "timestamp":
            parsed = datetime.strptime(value["value"], "%Y-%m-%dT%H:%M:%S.%fZ")
            return parsed.replace(tzinfo=timezone.utc)
        if value_type == "date":
            return date.fromisoformat(value["value"])
        if value_type == "wall_clock_time":
            return time.fromisoformat(value["value"])
        if value_type == "bytes":
            return base64.b64decode(value["value"], validate=True)
        if value_type == "text" and isinstance(value.get("value"), str):
            return value["value"]
        raise ValueError(f"Theta decoded artifact value type is invalid: {value_type}")


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
            sections.append(self._call("stock_history_ohlc", kwargs))
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
            contract_section = self._call(
                "option_list_contracts", list_kwargs,
                context={**list_kwargs, "decision_at": signal},
            )
            sections.append(contract_section)
            if contract_section.missing_evidence is not None:
                continue
            contracts = contract_section.dataframe
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
                sections.append(self._call("option_history_quote", quote_kwargs))
                sections.append(self._call("option_history_open_interest", common))
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
                sections.append(self._call("option_history_ohlc", ohlc_kwargs))
                sections.append(self._call("option_history_trade", history_kwargs))
        return sections

    def _call(
        self,
        endpoint: str,
        kwargs: Mapping[str, Any],
        *,
        context: Mapping[str, Any] | None = None,
    ) -> DecodedThetaSection:
        """Invoke one SDK endpoint once and preserve typed no-data provenance."""
        try:
            frame = getattr(self._client, endpoint)(**dict(kwargs))
            return DecodedThetaSection(endpoint, kwargs, frame)
        except NoDataFoundError:
            evidence_context = dict(context or kwargs)
            session_value = kwargs.get("date", kwargs.get("start_date"))
            if isinstance(session_value, datetime):
                session_value = session_value.date()
            if not isinstance(session_value, date):
                raise ValueError("Theta no-data response lacks a typed session date")
            evidence = ProviderMissingEvidence(
                provider="THETA_DATA",
                endpoint=endpoint,
                symbol=str(kwargs["symbol"]),
                session=session_value,
                reason_code="PROVIDER_NO_DATA",
                records_count=0,
                context=evidence_context,
            )
            return DecodedThetaSection(endpoint, evidence_context, evidence)

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
