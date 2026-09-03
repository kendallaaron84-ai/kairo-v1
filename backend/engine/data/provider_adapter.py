import hashlib
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from engine.validation.feed_loader import canonical_json_bytes


class ProviderArtifactType(StrEnum):
    OPAQUE_PROVIDER_ARTIFACT = "OPAQUE_PROVIDER_ARTIFACT"
    WIRE_PROVIDER_ARTIFACT = "WIRE_PROVIDER_ARTIFACT"
    DECODED_PROVIDER_ARTIFACT = "DECODED_PROVIDER_ARTIFACT"


@dataclass(frozen=True)
class ProviderTransportPayload:
    """Bytes returned by a transport, with an honest statement of their origin."""

    content: bytes
    mime_type: str
    artifact_type: ProviderArtifactType
    format_version: str
    serializer_version: str | None = None
    source_wire_sha256: str | None = None


@dataclass(frozen=True)
class RawProviderArtifact:
    """Immutable pre-normalization provider artifact and request provenance.

    The historical name is retained for compatibility with Migration 0023's
    ``RAW_PROVIDER_PAYLOAD`` role. ``artifact_type`` distinguishes actual wire
    bytes from an SDK-decoded provider artifact without adding a persistence
    authority.
    """

    provider_code: str
    request_kind: str
    content: bytes
    mime_type: str
    request_parameters: tuple[tuple[str, str], ...]
    artifact_type: ProviderArtifactType = ProviderArtifactType.OPAQUE_PROVIDER_ARTIFACT
    format_version: str = "PROVIDER_BYTES_UNCLASSIFIED-v1"
    serializer_version: str | None = None
    source_wire_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("provider artifact content must be non-empty bytes")
        if self.source_wire_sha256 is not None and (
            len(self.source_wire_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.source_wire_sha256)
        ):
            raise ValueError("source wire SHA-256 must be 64 lowercase hexadecimal characters")
        if (
            self.artifact_type is ProviderArtifactType.DECODED_PROVIDER_ARTIFACT
            and not self.serializer_version
        ):
            raise ValueError("decoded provider artifacts require a serializer version")

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def request_sha256(self) -> str:
        return hashlib.sha256(
            canonical_json_bytes(
                {
                    "provider_code": self.provider_code,
                    "request_kind": self.request_kind,
                    "request_parameters": dict(self.request_parameters),
                }
            )
        ).hexdigest()

    def registry_raw_fields(self) -> dict[str, Any]:
        """Fields consumed by the existing Migration 0023 dual-artifact registry.

        The decoded format is self-describing inside ``content``; it occupies the
        pre-normalization side of the frozen raw/normalized authority without
        adding a third database role.
        """

        return {"raw_bytes": self.content, "raw_mime_type": self.mime_type}


class HistoricalDataProviderAdapter(ABC):
    """Read-only acquisition boundary; persistence remains the 0023 authority."""

    provider_code: str

    @abstractmethod
    def fetch_equity_minute_bars(
        self, *, symbol: str, start_session: date, end_session: date
    ) -> RawProviderArtifact:
        raise NotImplementedError

    @abstractmethod
    def fetch_option_neighborhood(
        self,
        *,
        symbol: str,
        signal_at: datetime,
        target_dtes: tuple[int, ...] = (0, 1, 7, 14, 30),
        strikes_each_side: int = 10,
    ) -> RawProviderArtifact:
        raise NotImplementedError


ProviderTransport = Callable[[str, dict[str, Any]], bytes | ProviderTransportPayload]


class ThetaDataProviderAdapter(HistoricalDataProviderAdapter):
    """Theta acquisition request builder with injected, auditable transport.

    The adapter intentionally owns no network client. A caller must provide a
    transport and identify whether its bytes are wire, opaque, or a deterministic
    SDK-decoded artifact. The strike/DTE envelope is evidence acquisition metadata
    only and never invokes Strategy 001's resolver.
    """

    provider_code = "THETA_DATA"
    BAR_REQUEST_KIND = "EQUITY_RTH_1MIN_BARS"
    OPTION_REQUEST_KIND = "OPTION_STRIKE_NEIGHBORHOOD"

    def __init__(self, transport: ProviderTransport) -> None:
        self._transport = transport

    def fetch_equity_minute_bars(
        self, *, symbol: str, start_session: date, end_session: date
    ) -> RawProviderArtifact:
        if end_session < start_session:
            raise ValueError("pilot end session cannot precede start session")
        parameters = {
            "symbol": symbol.upper(),
            "start_session": start_session.isoformat(),
            "end_session": end_session.isoformat(),
            "interval_seconds": 60,
            "source_timestamp_convention": "INTERVAL_BEGIN",
            "completed_at_offset_seconds": 60,
            "session_scope": "RTH",
        }
        payload = self._transport(self.BAR_REQUEST_KIND, parameters)
        return self._artifact(self.BAR_REQUEST_KIND, payload, "application/octet-stream", parameters)

    def fetch_option_neighborhood(
        self,
        *,
        symbol: str,
        signal_at: datetime,
        target_dtes: tuple[int, ...] = (0, 1, 7, 14, 30),
        strikes_each_side: int = 10,
    ) -> RawProviderArtifact:
        return self.fetch_option_neighborhoods(
            symbol=symbol,
            signal_times=(signal_at,),
            target_dtes=target_dtes,
            strikes_each_side=strikes_each_side,
        )

    def fetch_option_neighborhoods(
        self,
        *,
        symbol: str,
        signal_times: tuple[datetime, ...],
        target_dtes: tuple[int, ...] = (0, 1, 7, 14, 30),
        strikes_each_side: int = 10,
    ) -> RawProviderArtifact:
        if not signal_times or any(value.tzinfo is None for value in signal_times):
            raise ValueError("signal timestamps must be non-empty and timezone-aware")
        if any(current <= previous for previous, current in zip(signal_times, signal_times[1:])):
            raise ValueError("signal timestamps must be strictly chronological")
        if tuple(sorted(set(target_dtes))) != target_dtes or any(value < 0 for value in target_dtes):
            raise ValueError("target DTE values must be unique, ordered, and non-negative")
        if strikes_each_side != 10:
            raise ValueError("Stage 1 acquisition envelope requires exactly ten strikes per side")
        parameters = {
            "symbol": symbol.upper(),
            "signal_times": ",".join(value.isoformat() for value in signal_times),
            "target_dtes": ",".join(str(item) for item in target_dtes),
            "strikes_each_side": strikes_each_side,
            "rights": "CALL,PUT",
            "selection_authority": "EVIDENCE_FETCH_ONLY",
            "open_interest_as_of": "PREVIOUS_SESSION_REPORTED_ON_REQUEST_SESSION",
            "missing_liquidity": "NULL_NEVER_ZERO",
            "volume_cutoff": "DECISION_TIMESTAMP_NO_EOD_LOOKAHEAD",
            "instrument_identity": "UNDERLYING_EXPIRATION_STRIKE_RIGHT",
        }
        payload = self._transport(self.OPTION_REQUEST_KIND, parameters)
        return self._artifact(self.OPTION_REQUEST_KIND, payload, "application/octet-stream", parameters)

    def _artifact(
        self,
        request_kind: str,
        payload: bytes | ProviderTransportPayload,
        default_mime_type: str,
        parameters: dict[str, Any],
    ) -> RawProviderArtifact:
        if isinstance(payload, bytes):
            payload = ProviderTransportPayload(
                content=payload,
                mime_type=default_mime_type,
                artifact_type=ProviderArtifactType.OPAQUE_PROVIDER_ARTIFACT,
                format_version="THETA_PROVIDER_BYTES_UNCLASSIFIED-v1",
            )
        if not isinstance(payload, ProviderTransportPayload) or not payload.content:
            raise ValueError("provider transport must return a non-empty typed provider payload")
        return RawProviderArtifact(
            provider_code=self.provider_code,
            request_kind=request_kind,
            content=payload.content,
            mime_type=payload.mime_type,
            request_parameters=tuple(sorted((key, str(value)) for key, value in parameters.items())),
            artifact_type=payload.artifact_type,
            format_version=payload.format_version,
            serializer_version=payload.serializer_version,
            source_wire_sha256=payload.source_wire_sha256,
        )
