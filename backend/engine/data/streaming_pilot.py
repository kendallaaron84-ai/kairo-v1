"""Bounded file-backed seams for the Stage 1 cloud qualification pilot.

The files in this module are staging media, never a second persistence authority.
Only ``HistoricalDatasetRegistry`` and the surrounding Cloud SQL transaction make
the staged bytes authoritative.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import pickle
import sqlite3
import struct
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, BinaryIO
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict

from app.domain.enums import OptionRight
from engine.data.corpus_qualifier import (
    CorpusQualificationEngine,
    CorpusQualificationManifest,
    PilotDecisionPoint,
    PilotWindow,
    QualificationMetrics,
    QualificationStatus,
)
from engine.data.option_enrollment import (
    CanonicalResolutionAccounting,
    HistoricalOptionEnrollmentGate,
    OptionEnrollmentFailure,
    RejectedOptionContract,
)
from engine.data.provider_adapter import (
    ProviderArtifactType,
    RawProviderArtifact,
    ThetaDataProviderAdapter,
)
from engine.data.theta_v3 import (
    DecodedThetaRecordSection,
    ThetaDecodedArtifactReader,
    ThetaDecodedArtifactSerializer,
)
from engine.validation.feed_loader import (
    HistoricalDatasetRegistry,
    StagedArtifact,
    canonical_json_bytes,
)
from engine.validation.models import CanonicalMarketBar, CanonicalOptionChainSnapshot
from engine.validation.session_calendar import SessionCalendarResolver


READ_CHUNK_SIZE = 8 * 1024 * 1024
RSS_BUDGET_MIB = 3072
EXTERNAL_MERGE_FAN_IN = 16


def _decimal_key(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f")


def file_identity(path: str | Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        while chunk := stream.read(READ_CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def staged_artifact(path: str | Path, mime_type: str) -> StagedArtifact:
    resolved = Path(path).resolve()
    digest, size = file_identity(resolved)
    return StagedArtifact(
        path=resolved,
        content_sha256=digest,
        byte_size=size,
        mime_type=mime_type,
    )


class CanonicalJsonArrayWriter(AbstractContextManager["CanonicalJsonArrayWriter"]):
    """Write canonical JSON array bytes one model at a time."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        )
        self._stream: BinaryIO | None = None
        self._first = True
        self.count = 0

    def __enter__(self) -> "CanonicalJsonArrayWriter":
        self._stream = self._temporary.open("xb")
        self._stream.write(b"[")
        return self

    def append(self, item: BaseModel | Mapping[str, Any]) -> None:
        if self._stream is None:
            raise RuntimeError("canonical array writer is not open")
        value = item.model_dump(mode="json") if isinstance(item, BaseModel) else item
        if not self._first:
            self._stream.write(b",")
        self._stream.write(canonical_json_bytes(value))
        self._first = False
        self.count += 1

    def __exit__(self, exc_type, exc, traceback) -> bool:
        assert self._stream is not None
        if exc_type is None:
            self._stream.write(b"]")
            self._stream.flush()
            os.fsync(self._stream.fileno())
        self._stream.close()
        self._stream = None
        if exc_type is None:
            self._temporary.replace(self.path)
        elif self._temporary.exists():
            self._temporary.unlink()
        return False


def iter_canonical_json_array(path: str | Path) -> Iterator[dict[str, Any]]:
    """Incrementally decode a canonical JSON array with a bounded input buffer."""
    decoder = json.JSONDecoder()
    with Path(path).open("r", encoding="utf-8") as stream:
        buffer = ""
        index = 0
        started = False
        ended = False
        while not ended:
            chunk = stream.read(1024 * 1024)
            if chunk:
                buffer = buffer[index:] + chunk
                index = 0
            elif index >= len(buffer):
                break
            while True:
                while index < len(buffer) and buffer[index] in " \r\n\t,":
                    index += 1
                if not started:
                    if index >= len(buffer):
                        break
                    if buffer[index] != "[":
                        raise ValueError("staged normalized stream is not a JSON array")
                    started = True
                    index += 1
                    continue
                while index < len(buffer) and buffer[index] in " \r\n\t,":
                    index += 1
                if index < len(buffer) and buffer[index] == "]":
                    ended = True
                    index += 1
                    break
                try:
                    value, index = decoder.raw_decode(buffer, index)
                except json.JSONDecodeError:
                    if chunk:
                        break
                    raise ValueError("staged normalized JSON array is truncated") from None
                if not isinstance(value, dict):
                    raise ValueError("staged normalized JSON array item is not an object")
                yield value
            if not chunk and not ended:
                raise ValueError("staged normalized JSON array is unterminated")
        if not started or not ended or buffer[index:].strip():
            raise ValueError("staged normalized JSON array has invalid trailing bytes")


def _read_frames(path: Path) -> Iterator[bytes]:
    magic = ThetaDecodedArtifactSerializer.MAGIC
    with path.open("rb") as stream:
        if stream.read(len(magic)) != magic:
            raise ValueError("Theta decoded staging artifact framing mismatch")
        while prefix := stream.read(8):
            if len(prefix) != 8:
                raise ValueError("Theta decoded staging frame prefix is truncated")
            length = struct.unpack(">Q", prefix)[0]
            frame = stream.read(length)
            if len(frame) != length:
                raise ValueError("Theta decoded staging frame is truncated")
            if canonical_json_bytes(json.loads(frame)) != frame:
                raise ValueError("Theta decoded staging frame is not canonical")
            yield frame


def iter_decoded_sections(artifact: StagedArtifact) -> Iterator[DecodedThetaRecordSection]:
    """Decode and validate one section frame at a time from a staged artifact."""
    digest, size = file_identity(artifact.path)
    if digest != artifact.content_sha256 or size != artifact.byte_size:
        raise ValueError("staged Theta artifact failed content verification")
    frames = _read_frames(artifact.path)
    try:
        header = json.loads(next(frames))
    except StopIteration:
        raise ValueError("Theta decoded staged artifact header is absent") from None
    expected_count = header.get("section_count")
    if not isinstance(expected_count, int) or expected_count < 0:
        raise ValueError("Theta decoded staged artifact section count is invalid")
    reader = ThetaDecodedArtifactReader()
    prior: bytes | None = None
    count = 0
    for frame in frames:
        if prior is not None and frame < prior:
            raise ValueError("Theta decoded staged sections are not canonically ordered")
        prior = frame
        count += 1
        yield reader._section(json.loads(frame))
    if count != expected_count:
        raise ValueError("Theta decoded staged artifact section count mismatch")
    reader._header(header, count)


class ThetaDecodedArtifactExternalMerger:
    """Merge provider artifacts with sequential, bounded fan-in frame passes."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)

    def merge(
        self,
        components: Sequence[StagedArtifact],
        *,
        request_kind: str,
        symbol: str,
        output_path: str | Path,
    ) -> StagedArtifact:
        if not components:
            raise ValueError(f"no provider artifacts were acquired for {symbol}")
        ordered = sorted(components, key=lambda item: item.content_sha256)
        wire_hashes: set[str] = set()
        section_count = 0
        sources: list[tuple[Path, bool]] = []
        for component in ordered:
            actual, size = file_identity(component.path)
            if actual != component.content_sha256 or size != component.byte_size:
                raise ValueError("component provider artifact failed staged verification")
            frames = _read_frames(component.path)
            try:
                header = json.loads(next(frames))
            except StopIteration:
                raise ValueError("Theta decoded component is missing its header") from None
            if (
                header.get("format_version") != ThetaDecodedArtifactSerializer.FORMAT_VERSION
                or header.get("serializer_version") != ThetaDecodedArtifactSerializer.SERIALIZER_VERSION
            ):
                raise ValueError("Theta decoded component version drift")
            component_count = header.get("section_count")
            if not isinstance(component_count, int) or component_count < 0:
                raise ValueError("Theta decoded component section count is invalid")
            ThetaDecodedArtifactReader()._header(header, component_count)
            wire_hashes.update(header.get("source_wire_sha256s", ()))
            section_count += component_count
            sources.append((component.path, True))
        temporary_files: set[Path] = set()
        round_index = 0
        try:
            while len(sources) > 1:
                next_round: list[tuple[Path, bool]] = []
                for group_index in range(0, len(sources), EXTERNAL_MERGE_FAN_IN):
                    group = sources[group_index:group_index + EXTERNAL_MERGE_FAN_IN]
                    if len(group) == 1:
                        next_round.append(group[0])
                        continue
                    target = self.workspace / (
                        f"frames-{symbol}-{request_kind}-{round_index}-{group_index // EXTERNAL_MERGE_FAN_IN}.bin"
                    )
                    self._merge_group(group, target)
                    temporary_files.add(target)
                    next_round.append((target, False))
                for path, is_theta in sources:
                    if not is_theta and path not in {item[0] for item in next_round}:
                        path.unlink(missing_ok=True)
                        temporary_files.discard(path)
                sources = next_round
                round_index += 1
            serializer = ThetaDecodedArtifactSerializer()
            acquisition_request = {
                "request_kind": request_kind,
                "symbol": symbol,
                "component_content_sha256s": ",".join(item.content_sha256 for item in ordered),
            }
            header = {
                "artifact_type": ProviderArtifactType.DECODED_PROVIDER_ARTIFACT,
                "format_version": serializer.FORMAT_VERSION,
                "serializer_version": serializer.SERIALIZER_VERSION,
                "field_order": "UNICODE_CODEPOINT_ASCENDING",
                "row_order": "LEXICOGRAPHIC_CANONICAL_TYPED_ROW_BYTES",
                "timestamp_encoding": "UTC_RFC3339_MICROSECONDS_Z",
                "null_encoding": {"type": "null"},
                "integer_encoding": "BASE10_STRING",
                "float_encoding": "PYTHON_FLOAT_HEX_IEEE754_BINARY64",
                "decimal_encoding": "SIGN_DIGITS_EXPONENT",
                "framing": "MAGIC_THEN_UINT64_BE_LENGTH_PREFIXED_CANONICAL_JSON_FRAMES",
                "acquisition_request": serializer._canonical_mapping(acquisition_request),
                "source_wire_sha256s": sorted(wire_hashes),
                "section_count": section_count,
            }
            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_name(
                f".{output.name}.{os.getpid()}.{uuid4().hex}.tmp"
            )
            actual_count = 0
            with temporary.open("xb") as stream:
                stream.write(serializer.MAGIC)
                header_frame = canonical_json_bytes(header)
                stream.write(struct.pack(">Q", len(header_frame)))
                stream.write(header_frame)
                for frame in self._source_frames(sources[0]):
                    stream.write(struct.pack(">Q", len(frame)))
                    stream.write(frame)
                    actual_count += 1
                stream.flush()
                os.fsync(stream.fileno())
            if actual_count != section_count:
                temporary.unlink(missing_ok=True)
                raise ValueError("Theta decoded component section count mismatch")
            temporary.replace(output)
            return staged_artifact(output, serializer.MIME_TYPE)
        finally:
            for path in temporary_files:
                path.unlink(missing_ok=True)

    @staticmethod
    def _length_frames(path: Path) -> Iterator[bytes]:
        with path.open("rb") as stream:
            while prefix := stream.read(8):
                if len(prefix) != 8:
                    raise ValueError("external merge frame prefix is truncated")
                size = struct.unpack(">Q", prefix)[0]
                frame = stream.read(size)
                if len(frame) != size:
                    raise ValueError("external merge frame is truncated")
                yield frame

    @classmethod
    def _source_frames(cls, source: tuple[Path, bool]) -> Iterator[bytes]:
        path, is_theta = source
        frames = _read_frames(path) if is_theta else cls._length_frames(path)
        if is_theta:
            next(frames)
        prior: bytes | None = None
        for frame in frames:
            if prior is not None and frame < prior:
                raise ValueError("external merge source frames are not ordered")
            prior = frame
            yield frame

    @classmethod
    def _merge_group(
        cls,
        sources: Sequence[tuple[Path, bool]],
        target: Path,
    ) -> None:
        iterators = [iter(cls._source_frames(source)) for source in sources]
        values: list[tuple[bytes, int]] = []
        for index, iterator in enumerate(iterators):
            first = next(iterator, None)
            if first is not None:
                heapq.heappush(values, (first, index))
        temporary = target.with_name(
            f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp"
        )
        with temporary.open("xb") as stream:
            while values:
                value, index = heapq.heappop(values)
                stream.write(struct.pack(">Q", len(value)))
                stream.write(value)
                following = next(iterators[index], None)
                if following is not None:
                    heapq.heappush(values, (following, index))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)


@dataclass(frozen=True)
class StagedProviderUnit:
    unit_key: str
    symbol: str
    session: date
    signal_at: datetime | None
    artifact: StagedArtifact
    record_count: int


class OptionDiscoverySpool(AbstractContextManager["OptionDiscoverySpool"]):
    """Disk-backed global discovery deduplication and accepted-key index."""

    def __init__(self, path: str | Path, underlying_symbol: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.underlying_symbol = underlying_symbol
        self.connection = sqlite3.connect(self.path)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS valid_discoveries (expiration TEXT, strike TEXT, right TEXT, "
            "observed_at TEXT, raw BLOB NOT NULL, PRIMARY KEY(expiration, strike, right))"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS rejected_discoveries (discovery_key TEXT PRIMARY KEY, raw BLOB NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS accepted (expiration TEXT, strike TEXT, right TEXT, "
            "PRIMARY KEY(expiration, strike, right))"
        )

    def ingest_sections(self, sections: Iterable[Any], *, commit: bool = True) -> None:
        gate = HistoricalOptionEnrollmentGate
        for section in sections:
            if section.endpoint != "option_history_quote":
                continue
            for row in section.records:
                raw = {
                    "symbol": section.parameters.get("symbol", self.underlying_symbol),
                    "expiration": section.parameters.get("expiration"),
                    **row,
                }
                discovery_key = gate._discovery_key(raw)
                try:
                    key, observed_at = gate._parse(raw, self.underlying_symbol)
                    expiration, strike, right = key
                    existing = self.connection.execute(
                        "SELECT observed_at FROM valid_discoveries WHERE expiration=? AND strike=? AND right=?",
                        (expiration.isoformat(), _decimal_key(strike), right.value),
                    ).fetchone()
                    rendered = observed_at.astimezone(timezone.utc).isoformat()
                    if existing is None or rendered < existing[0]:
                        self.connection.execute(
                            "INSERT OR REPLACE INTO valid_discoveries VALUES (?, ?, ?, ?, ?)",
                            (
                                expiration.isoformat(),
                                _decimal_key(strike),
                                right.value,
                                rendered,
                                sqlite3.Binary(pickle.dumps(raw, protocol=5)),
                            ),
                        )
                except OptionEnrollmentFailure:
                    self.connection.execute(
                        "INSERT OR IGNORE INTO rejected_discoveries VALUES (?, ?)",
                        (discovery_key, sqlite3.Binary(pickle.dumps(raw, protocol=5))),
                    )
        if commit:
            self.connection.commit()

    def commit(self) -> None:
        self.connection.commit()

    def enrollment_batches(self, batch_size: int = 500) -> Iterator[list[Mapping[str, Any]]]:
        cursor = self.connection.execute(
            "SELECT raw FROM valid_discoveries ORDER BY expiration, CAST(strike AS REAL), right"
        )
        while rows := cursor.fetchmany(batch_size):
            yield [pickle.loads(row[0]) for row in rows]
        cursor = self.connection.execute(
            "SELECT raw FROM rejected_discoveries ORDER BY discovery_key"
        )
        while rows := cursor.fetchmany(batch_size):
            yield [pickle.loads(row[0]) for row in rows]

    def record_accepted(self, keys: Iterable[tuple[date, Decimal, OptionRight]]) -> None:
        self.connection.executemany(
            "INSERT OR IGNORE INTO accepted VALUES (?, ?, ?)",
            ((expiry.isoformat(), _decimal_key(strike), right.value) for expiry, strike, right in keys),
        )
        self.connection.commit()

    def accepted_for_sections(self, sections: Iterable[Any]) -> frozenset[tuple[date, Decimal, OptionRight]]:
        keys: set[tuple[date, Decimal, OptionRight]] = set()
        for section in sections:
            if section.endpoint != "option_history_quote":
                continue
            default_expiry = HistoricalOptionEnrollmentGate._parse
            for row in section.records:
                raw = {
                    "symbol": section.parameters.get("symbol", self.underlying_symbol),
                    "expiration": section.parameters.get("expiration"),
                    **row,
                }
                try:
                    key, _ = default_expiry(raw, self.underlying_symbol)
                except OptionEnrollmentFailure:
                    continue
                expiry, strike, right = key
                present = self.connection.execute(
                    "SELECT 1 FROM accepted WHERE expiration=? AND strike=? AND right=?",
                    (expiry.isoformat(), _decimal_key(strike), right.value),
                ).fetchone()
                if present:
                    keys.add(key)
        return frozenset(keys)

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.connection.close()
        return False


class SessionLiquidityIndex(AbstractContextManager["SessionLiquidityIndex"]):
    """Disk-backed latest previous-session OI index across option units."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS oi (session TEXT, expiration TEXT, strike TEXT, right TEXT, "
            "observed_at TEXT, row BLOB NOT NULL, PRIMARY KEY(session, expiration, strike, right))"
        )

    def ingest_sections(self, sections: Iterable[Any], *, commit: bool = True) -> None:
        for section in sections:
            if section.endpoint != "option_history_open_interest":
                continue
            request_date = str(section.parameters.get("date"))[:10]
            default_expiration = str(section.parameters.get("expiration"))[:10]
            for row in section.records:
                try:
                    expiration_value = row.get("expiration", default_expiration)
                    expiration = (
                        expiration_value.date().isoformat()
                        if isinstance(expiration_value, datetime)
                        else expiration_value.isoformat()
                        if isinstance(expiration_value, date)
                        else date.fromisoformat(str(expiration_value)[:10]).isoformat()
                    )
                    strike = _decimal_key(Decimal(str(row["strike"])))
                    raw_right = str(row["right"]).upper()
                    right = "CALL" if raw_right in {"CALL", "C"} else "PUT" if raw_right in {"PUT", "P"} else None
                    observed = row.get("timestamp")
                    if right is None or not isinstance(observed, datetime) or observed.tzinfo is None:
                        continue
                    observed_at = observed.astimezone(timezone.utc).isoformat()
                except (KeyError, ValueError):
                    continue
                prior = self.connection.execute(
                    "SELECT observed_at FROM oi WHERE session=? AND expiration=? AND strike=? AND right=?",
                    (request_date, expiration, strike, right),
                ).fetchone()
                if prior is None or observed_at > prior[0]:
                    self.connection.execute(
                        "INSERT OR REPLACE INTO oi VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            request_date,
                            expiration,
                            strike,
                            right,
                            observed_at,
                            sqlite3.Binary(pickle.dumps(row, protocol=5)),
                        ),
                    )
        if commit:
            self.connection.commit()

    def commit(self) -> None:
        self.connection.commit()

    def sections_for(
        self,
        request_date: date,
        contract_keys: Iterable[tuple[date, Decimal, OptionRight]] | None = None,
    ) -> tuple[DecodedThetaRecordSection, ...]:
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        if contract_keys is None:
            rows = self.connection.execute(
                "SELECT expiration, row FROM oi WHERE session=? ORDER BY expiration, CAST(strike AS REAL), right",
                (request_date.isoformat(),),
            )
        else:
            selected = []
            for expiration, strike, right in sorted(
                set(contract_keys), key=lambda item: (item[0], item[1], item[2].value)
            ):
                selected.extend(self.connection.execute(
                    "SELECT expiration, row FROM oi WHERE session=? AND expiration=? AND strike=? AND right=?",
                    (
                        request_date.isoformat(), expiration.isoformat(),
                        _decimal_key(strike), right.value,
                    ),
                ))
            rows = selected
        for expiration, raw in rows:
            grouped[expiration].append(pickle.loads(raw))
        return tuple(
            DecodedThetaRecordSection(
                endpoint="option_history_open_interest",
                parameters={"date": request_date, "expiration": date.fromisoformat(expiration)},
                fields=tuple(sorted({key for row in rows for key in row})),
                records=tuple(rows),
            )
            for expiration, rows in sorted(grouped.items())
        )

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.connection.close()
        return False


def normalize_staged_option_unit(
    unit: StagedProviderUnit,
    *,
    normalizer: Any,
    underlying_instrument_id: UUID,
    symbol: str,
    discovery_spool: OptionDiscoverySpool,
    liquidity_index: SessionLiquidityIndex,
) -> tuple[CanonicalOptionChainSnapshot, ...]:
    """Normalize exactly one option unit, then release its decoded representation."""
    content = unit.artifact.path.read_bytes()
    artifact = RawProviderArtifact(
        provider_code="THETA_DATA",
        request_kind=ThetaDataProviderAdapter.OPTION_REQUEST_KIND,
        content=content,
        mime_type=unit.artifact.mime_type,
        request_parameters=(("unit_key", unit.unit_key),),
        artifact_type=ProviderArtifactType.DECODED_PROVIDER_ARTIFACT,
        format_version=ThetaDecodedArtifactSerializer.FORMAT_VERSION,
        serializer_version=ThetaDecodedArtifactSerializer.SERIALIZER_VERSION,
    )
    decoded = ThetaDecodedArtifactReader().read_provider_artifact(artifact)
    accepted = discovery_spool.accepted_for_sections(decoded.sections)
    sections = (
        *(section for section in decoded.sections
          if section.endpoint != "option_history_open_interest"),
        *liquidity_index.sections_for(unit.session, accepted),
    )
    return normalizer.normalize_theta_option_sections(
        sections,
        underlying_instrument_id=underlying_instrument_id,
        symbol=symbol,
        accepted_contract_keys=accepted,
    )


class StagedCorpusQualificationInput(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    provider_code: str
    start_session: date
    end_session: date
    symbols: tuple[str, ...]
    bar_artifacts: tuple[StagedArtifact, ...]
    option_snapshot_artifacts: tuple[StagedArtifact, ...]
    decision_points: tuple[PilotDecisionPoint, ...]
    raw_artifact_sha256s: tuple[str, ...]
    normalized_dataset_manifest_sha256: str
    resolution_accounting: CanonicalResolutionAccounting
    target_dtes: tuple[int, ...] = (0, 1, 7, 14, 30)
    strikes_each_side: int = 10


def _verified_items(
    artifacts: Sequence[StagedArtifact], model: type[BaseModel]
) -> Iterator[Any]:
    for artifact in artifacts:
        digest, size = file_identity(artifact.path)
        if digest != artifact.content_sha256 or size != artifact.byte_size:
            raise ValueError("staged qualification artifact failed content verification")
        for value in iter_canonical_json_array(artifact.path):
            yield model.model_validate(value)


def qualify_staged(
    engine: CorpusQualificationEngine,
    evidence: StagedCorpusQualificationInput,
) -> CorpusQualificationManifest:
    """Streaming-equivalent sibling of ``CorpusQualificationEngine.qualify``."""
    if evidence.end_session < evidence.start_session:
        raise ValueError("pilot end session cannot precede start session")
    if not evidence.symbols or len(set(evidence.symbols)) != len(evidence.symbols):
        raise ValueError("pilot symbols must be non-empty and unique")
    if evidence.target_dtes != engine.REQUIRED_TARGET_DTES:
        raise ValueError("Stage 1 requires the frozen 0/1/7/14/30 DTE envelope")
    if evidence.strikes_each_side != engine.REQUIRED_STRIKES_EACH_SIDE:
        raise ValueError("Stage 1 requires ten strikes on each side of spot")
    hash_values = (*evidence.raw_artifact_sha256s, evidence.normalized_dataset_manifest_sha256)
    if not evidence.raw_artifact_sha256s or any(
        len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
        for value in hash_values
    ):
        raise ValueError("qualification evidence hashes must be lowercase SHA-256 values")
    sessions = engine.calendar.sessions(evidence.start_session, evidence.end_session)
    if not sessions:
        raise ValueError("pilot window contains no canonical sessions")
    expected_minutes = sum(
        int((closed - opened).total_seconds() // 60) for _, opened, closed in sessions
    )
    bar_times: dict[str, set[datetime]] = defaultdict(set)
    prior_bar: dict[str, datetime] = {}
    violations = 0
    for bar in _verified_items(evidence.bar_artifacts, CanonicalMarketBar):
        local_date = bar.completed_at.astimezone(engine.calendar.eastern).date()
        if evidence.start_session <= local_date <= evidence.end_session:
            bar_times[bar.symbol].add(bar.completed_at)
        if bar.completed_at <= bar.interval_start_at:
            violations += 1
        if bar.symbol in prior_bar and bar.completed_at <= prior_bar[bar.symbol]:
            violations += 1
        prior_bar[bar.symbol] = bar.completed_at
    completeness = min(
        engine._percentage(min(len(bar_times[symbol]), expected_minutes), expected_minutes)
        for symbol in evidence.symbols
    )
    decisions = {
        (row.underlying_instrument_id, row.signal_at): row
        for row in evidence.decision_points
    }
    exact_snapshot_keys: set[tuple[UUID, datetime]] = set()
    future_times: dict[UUID, list[datetime]] = defaultdict(list)
    prior_snapshot: dict[str, datetime] = {}
    complete_decisions = 0
    has_quotes = False
    all_quote_depth = True
    has_liquidity = False
    for snapshot in _verified_items(
        evidence.option_snapshot_artifacts, CanonicalOptionChainSnapshot
    ):
        if (
            snapshot.underlying_symbol in prior_snapshot
            and snapshot.canonical_completed_at <= prior_snapshot[snapshot.underlying_symbol]
        ):
            violations += 1
        prior_snapshot[snapshot.underlying_symbol] = snapshot.canonical_completed_at
        key = (snapshot.underlying_instrument_id, snapshot.canonical_completed_at)
        exact_snapshot_keys.add(key)
        future_times[snapshot.underlying_instrument_id].append(snapshot.canonical_completed_at)
        decision = decisions.get(key)
        if decision is not None and snapshot.underlying_symbol == decision.symbol:
            if engine._has_full_neighborhood(snapshot, decision):
                complete_decisions += 1
        for contract in snapshot.contracts:
            has_quotes = True
            all_quote_depth = all_quote_depth and all((
                contract.bid_price > 0,
                contract.ask_price > contract.bid_price,
                contract.bid_size > 0,
                contract.ask_size > 0,
            ))
            has_liquidity = has_liquidity or (
                (contract.volume is not None and contract.volume > 0)
                or (contract.open_interest is not None and contract.open_interest > 0)
            )
    for decision in evidence.decision_points:
        key = (decision.underlying_instrument_id, decision.signal_at)
        if key not in exact_snapshot_keys and any(
            value > decision.signal_at for value in future_times[decision.underlying_instrument_id]
        ):
            violations += 1
    decision_pct = engine._percentage(complete_decisions, len(evidence.decision_points))
    resolution_pct = evidence.resolution_accounting.resolution_percentage
    fidelity = (
        "TIER_1_QUOTE_DEPTH"
        if has_quotes and all_quote_depth
        else "TIER_2_TRADE_HISTORY"
        if has_quotes and has_liquidity
        else "TIER_3_BAR_ONLY"
    )
    metrics = QualificationMetrics(
        underlying_bar_completeness_pct=completeness,
        underlying_status=engine._threshold(completeness, Decimal("99.80"), Decimal("99.00")),
        strategy_signal_count=len(evidence.decision_points),
        decision_point_complete_evidence_count=complete_decisions,
        decision_point_evidence_pct=decision_pct,
        decision_evidence_status=engine._threshold(decision_pct, Decimal("95.00"), Decimal("90.00")),
        causal_timestamp_violations_count=violations,
        causal_status=QualificationStatus.PASS if violations == 0 else QualificationStatus.FAIL,
        canonical_contract_resolution_pct=resolution_pct,
        resolution_status=QualificationStatus.PASS if resolution_pct == Decimal("100.00") else QualificationStatus.FAIL,
        resolution_accounting=evidence.resolution_accounting,
        assigned_fidelity_tier=fidelity,
        fidelity_status={
            "TIER_1_QUOTE_DEPTH": QualificationStatus.PASS,
            "TIER_2_TRADE_HISTORY": QualificationStatus.REVIEW,
            "TIER_3_BAR_ONLY": QualificationStatus.FAIL,
        }[fidelity],
    )
    verdict = engine._overall(metrics)
    raw_hash = hashlib.sha256(
        canonical_json_bytes(sorted(evidence.raw_artifact_sha256s))
    ).hexdigest()
    window = PilotWindow(
        start_session=evidence.start_session,
        end_session=evidence.end_session,
        total_calendar_sessions=len(sessions),
        rth_expected_minutes=expected_minutes,
    )
    body = {
        "qualification_policy_version": engine.POLICY_VERSION,
        "provider_code": evidence.provider_code,
        "pilot_window": window.model_dump(mode="json"),
        "metrics": metrics.model_dump(mode="json"),
        "overall_qualification_verdict": verdict,
        "raw_artifacts_manifest_sha256": raw_hash,
        "normalized_dataset_manifest_sha256": evidence.normalized_dataset_manifest_sha256,
    }
    digest = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
    return CorpusQualificationManifest(
        qualification_manifest_id=uuid5(NAMESPACE_URL, f"kairo:corpus-qualification:{digest}"),
        qualification_manifest_sha256=digest,
        qualification_policy_version=engine.POLICY_VERSION,
        provider_code=evidence.provider_code,
        pilot_window=window,
        metrics=metrics,
        overall_qualification_verdict=verdict,
        raw_artifacts_manifest_sha256=raw_hash,
        normalized_dataset_manifest_sha256=evidence.normalized_dataset_manifest_sha256,
    )


def register_staged_dataset(
    registry: HistoricalDatasetRegistry, **kwargs: Any
):
    return registry.register_staged_dataset(**kwargs)


def persist_staged_pilot_atomically(
    engine,
    persistence: Callable[[Any], Any],
):
    """Execute a staged persistence callback inside exactly one DB transaction."""
    from sqlalchemy.orm import Session

    with Session(engine, expire_on_commit=False) as session, session.begin():
        result = persistence(session)
    return result


def current_rss_mib() -> float:
    """Return current process RSS without importing an optional telemetry package."""
    proc_statm = Path("/proc/self/statm")
    if proc_statm.exists():
        resident_pages = int(proc_statm.read_text(encoding="ascii").split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    if sys.platform == "win32":
        import ctypes

        class MemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        get_process = ctypes.windll.kernel32.GetCurrentProcess
        get_process.restype = ctypes.c_void_p
        get_memory = ctypes.windll.psapi.GetProcessMemoryInfo
        get_memory.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(MemoryCounters),
            ctypes.c_ulong,
        )
        get_memory.restype = ctypes.c_int
        succeeded = get_memory(get_process(), ctypes.byref(counters), counters.cb)
        if not succeeded:
            raise OSError("GetProcessMemoryInfo failed")
        return counters.WorkingSetSize / (1024 * 1024)
    try:
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return value / (1024 * 1024) if sys.platform == "darwin" else value / 1024
    except ImportError:
        raise RuntimeError("RSS telemetry is unavailable on this platform") from None


def enforce_rss_budget(checkpoint: str, budget_mib: int = RSS_BUDGET_MIB) -> float:
    rss = current_rss_mib()
    if rss >= budget_mib:
        raise MemoryError(f"RSS budget exceeded at {checkpoint}: {rss:.1f} MiB >= {budget_mib} MiB")
    return rss


def release_unit_memory() -> None:
    """Release cyclic objects and return free glibc arenas after one unit."""
    import gc

    gc.collect()
    if sys.platform.startswith("linux"):
        import ctypes

        libc = ctypes.CDLL(None)
        malloc_trim = getattr(libc, "malloc_trim", None)
        if malloc_trim is not None:
            malloc_trim(0)


@dataclass
class RssBudgetTracker:
    """Fail closed on the hard ceiling or Class-B steady-state growth."""

    budget_mib: int = RSS_BUDGET_MIB
    max_slope_mib_per_unit: float = 0.01
    max_window_growth_mib: float = 128.0

    def __post_init__(self) -> None:
        self.samples: list[tuple[int, float, str]] = []

    def sample(self, checkpoint: str, cumulative_units: int) -> float:
        rss = enforce_rss_budget(checkpoint, self.budget_mib)
        if cumulative_units < 0:
            raise ValueError("cumulative unit count cannot be negative")
        self.samples.append((cumulative_units, rss, checkpoint))
        return rss

    def assert_bounded_growth(self) -> None:
        ordered = sorted(self.samples, key=lambda item: item[0])
        if len(ordered) < 2 or ordered[-1][0] == ordered[0][0]:
            return
        unit_delta = ordered[-1][0] - ordered[0][0]
        growth = max(0.0, ordered[-1][1] - ordered[0][1])
        if growth / unit_delta > self.max_slope_mib_per_unit:
            raise MemoryError("steady-state RSS slope indicates Class-B accumulation")
        window = min(3, len(ordered))
        first = sorted(item[1] for item in ordered[:window])[window // 2]
        last = sorted(item[1] for item in ordered[-window:])[window // 2]
        if last - first > self.max_window_growth_mib:
            raise MemoryError("steady-state RSS window growth exceeds the safety margin")
