import hashlib
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from engine.validation.feed_loader import canonical_json_bytes


@dataclass(frozen=True)
class RawProviderArtifact:
    """Exact provider response bytes and deterministic request provenance."""

    provider_code: str
    request_kind: str
    content: bytes
    mime_type: str
    request_parameters: tuple[tuple[str, str], ...]

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


ProviderTransport = Callable[[str, dict[str, Any]], bytes]


class ThetaDataProviderAdapter(HistoricalDataProviderAdapter):
    """Theta acquisition request builder with injected, auditable transport.

    The adapter intentionally owns no network client. A caller must provide a
    transport, which makes exact response bytes available for content-addressed
    persistence before normalization. The strike/DTE envelope is evidence
    acquisition metadata only and never invokes Strategy 001's resolver.
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
            "session_scope": "RTH",
        }
        content = self._transport(self.BAR_REQUEST_KIND, parameters)
        return self._artifact(self.BAR_REQUEST_KIND, content, "text/csv", parameters)

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
        }
        content = self._transport(self.OPTION_REQUEST_KIND, parameters)
        return self._artifact(self.OPTION_REQUEST_KIND, content, "application/json", parameters)

    def _artifact(
        self, request_kind: str, content: bytes, mime_type: str, parameters: dict[str, Any]
    ) -> RawProviderArtifact:
        if not isinstance(content, bytes) or not content:
            raise ValueError("provider transport must return non-empty exact response bytes")
        return RawProviderArtifact(
            provider_code=self.provider_code,
            request_kind=request_kind,
            content=content,
            mime_type=mime_type,
            request_parameters=tuple(sorted((key, str(value)) for key, value in parameters.items())),
        )
