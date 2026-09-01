import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.historical import HistoricalMarketArtifact, HistoricalMarketDataset, HistoricalMarketDatasetSymbol
from engine.execution.replay_orchestrator import ReplayOptionChainEvent, ResearchReplayInput
from engine.validation.adapter import HistoricalReplayAdapter
from engine.validation.models import CanonicalMarketBar, CanonicalOptionChainSnapshot, StreamRole


@dataclass(frozen=True)
class LoadedHistoricalStream:
    stream_ordinal: int
    stream_role: StreamRole
    symbol: str
    instrument_id: UUID
    normalized_content_sha256: str
    replay_input: ResearchReplayInput | None = None
    option_chains: tuple[ReplayOptionChainEvent, ...] = ()


class HistoricalDatasetStreamLoader:
    """Hydrates immutable normalized artifacts in persisted stream ordinal order."""

    def __init__(self, session: Session, artifact_reader: Callable[[HistoricalMarketArtifact], bytes] | None = None) -> None:
        self.session = session
        self.artifact_reader = artifact_reader or self._read_file_artifact

    def load(self, dataset_id: UUID) -> tuple[LoadedHistoricalStream, ...]:
        if self.session.get(HistoricalMarketDataset, dataset_id) is None:
            raise ValueError("historical dataset does not exist")
        entries = list(self.session.scalars(
            select(HistoricalMarketDatasetSymbol)
            .where(HistoricalMarketDatasetSymbol.dataset_id == dataset_id)
            .order_by(HistoricalMarketDatasetSymbol.stream_ordinal)
        ))
        result: list[LoadedHistoricalStream] = []
        adapter = HistoricalReplayAdapter(str(dataset_id))
        for entry in entries:
            artifact = self.session.get(HistoricalMarketArtifact, entry.normalized_artifact_id)
            if artifact is None or artifact.content_sha256 != entry.normalized_content_sha256 or artifact.artifact_role != "NORMALIZED_RESEARCH_STREAM":
                raise ValueError("normalized stream artifact lineage does not resolve")
            payload = json.loads(self.artifact_reader(artifact))
            role = StreamRole(entry.stream_role)
            if role in {StreamRole.UNDERLYING_SIGNAL_BARS, StreamRole.CONTEXT_MACRO_SERIES}:
                bars = tuple(CanonicalMarketBar.model_validate(item) for item in payload)
                replay = adapter.bars(bars, stream_role=StreamRole.UNDERLYING_SIGNAL_BARS)
                result.append(LoadedHistoricalStream(entry.stream_ordinal, role, entry.symbol, entry.instrument_id, entry.normalized_content_sha256, replay_input=replay))
            elif role is StreamRole.OPTION_CHAIN_QUOTES:
                snapshots = tuple(CanonicalOptionChainSnapshot.model_validate(item) for item in payload)
                result.append(LoadedHistoricalStream(entry.stream_ordinal, role, entry.symbol, entry.instrument_id, entry.normalized_content_sha256, option_chains=adapter.option_chains(snapshots)))
        return tuple(result)

    @staticmethod
    def _read_file_artifact(artifact: HistoricalMarketArtifact) -> bytes:
        parsed = urlparse(artifact.storage_uri)
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
            raise ValueError("default historical artifact reader supports only file URIs")
        return Path(url2pathname(unquote(parsed.path))).read_bytes()
