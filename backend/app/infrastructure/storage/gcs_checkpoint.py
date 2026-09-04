"""Content-addressed GCS checkpoints for bounded historical acquisition units."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Protocol, TypeVar


THETA_DECODED_FORMAT_VERSION = "THETA_PROTOBUF_DECODED-v1"


class Blob(Protocol):
    def exists(self) -> bool: ...
    def download_as_bytes(self) -> bytes: ...
    def upload_from_string(
        self,
        data: bytes,
        *,
        content_type: str,
        if_generation_match: int,
    ) -> None: ...


class Bucket(Protocol):
    def blob(self, name: str) -> Blob: ...


class StorageClient(Protocol):
    def bucket(self, bucket_name: str) -> Bucket: ...


class ProviderArtifact(Protocol):
    content: bytes
    format_version: str
    serializer_version: str | None


ArtifactT = TypeVar("ArtifactT", bound=ProviderArtifact)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def acquisition_unit_key(
    *,
    provider: str,
    endpoint: str,
    symbol: str,
    session: date,
    signal_at: datetime | None,
    target_dtes: Sequence[int],
    strikes_each_side: int,
    serializer_version: str,
    acquisition_policy_version: str,
) -> str:
    """Return the frozen SHA-256 identity for one acquisition unit."""
    identity = (
        f"{provider}|{endpoint}|{symbol}|{session}|{signal_at}|"
        f"{sorted(target_dtes)}|{strikes_each_side}|"
        f"{serializer_version}|{acquisition_policy_version}"
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True)
class AcquisitionUnit:
    provider: str
    endpoint: str
    symbol: str
    session: date
    signal_at: datetime | None
    target_dtes: tuple[int, ...]
    strikes_each_side: int
    serializer_version: str
    acquisition_policy_version: str

    def __post_init__(self) -> None:
        if not self.provider or not self.endpoint or not self.symbol:
            raise ValueError("provider, endpoint, and symbol are required")
        if tuple(sorted(set(self.target_dtes))) != self.target_dtes:
            raise ValueError("target DTEs must be unique and ascending")
        if self.strikes_each_side < 0:
            raise ValueError("strikes_each_side cannot be negative")
        if self.signal_at is not None and self.signal_at.tzinfo is None:
            raise ValueError("signal_at must be timezone-aware")

    @property
    def unit_key(self) -> str:
        return acquisition_unit_key(**asdict(self))


@dataclass(frozen=True)
class CheckpointMetadata:
    unit_key: str
    content_sha256: str
    format_version: str
    serializer_version: str
    provider: str
    endpoint: str
    symbol: str
    session: str
    signal_at: str | None
    sealed_at: str
    record_count: int

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(asdict(self))


class GCSCheckpointStore:
    """Atomic GCS seal/resume boundary with byte-exact provider artifacts."""

    def __init__(self, client: StorageClient, bucket_name: str) -> None:
        if not bucket_name:
            raise ValueError("GCS bucket name is required")
        self._bucket = client.bucket(bucket_name.removeprefix("gs://"))

    @classmethod
    def from_default_credentials(cls, bucket_name: str) -> "GCSCheckpointStore":
        from google.cloud import storage

        return cls(storage.Client(), bucket_name)

    @staticmethod
    def artifact_path(content_sha256: str) -> str:
        _validate_digest(content_sha256)
        return f"artifacts/theta/sha256/{content_sha256[:2]}/{content_sha256}.bin"

    @staticmethod
    def checkpoint_path(unit: AcquisitionUnit) -> str:
        symbol = _safe_component(unit.symbol, "symbol")
        return f"checkpoints/{symbol}/{unit.session.isoformat()}/{unit.unit_key}.json"

    def checkpoint_exists(self, unit: AcquisitionUnit) -> bool:
        return self._bucket.blob(self.checkpoint_path(unit)).exists()

    def load(self, unit: AcquisitionUnit) -> CheckpointMetadata:
        blob = self._bucket.blob(self.checkpoint_path(unit))
        if not blob.exists():
            raise FileNotFoundError(f"checkpoint is absent for unit {unit.unit_key}")
        content = blob.download_as_bytes()
        try:
            value = json.loads(content)
            metadata = CheckpointMetadata(**value)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
            raise ValueError("checkpoint metadata is invalid") from error
        if metadata.canonical_bytes() != content:
            raise ValueError("checkpoint metadata is not canonical")
        self._validate_metadata(unit, metadata)
        artifact_blob = self._bucket.blob(self.artifact_path(metadata.content_sha256))
        if not artifact_blob.exists():
            raise ValueError("checkpoint references an absent provider artifact")
        artifact = artifact_blob.download_as_bytes()
        if hashlib.sha256(artifact).hexdigest() != metadata.content_sha256:
            raise ValueError("checkpoint provider artifact SHA-256 mismatch")
        return metadata

    def seal(
        self,
        unit: AcquisitionUnit,
        artifact: ProviderArtifact,
        *,
        record_count: int,
        sealed_at: datetime,
    ) -> CheckpointMetadata:
        if artifact.format_version != THETA_DECODED_FORMAT_VERSION:
            raise ValueError("provider artifact format must be THETA_PROTOBUF_DECODED-v1")
        if artifact.serializer_version != unit.serializer_version:
            raise ValueError("provider artifact serializer version mismatch")
        if not isinstance(artifact.content, bytes) or not artifact.content:
            raise ValueError("provider artifact content must be non-empty bytes")
        if record_count < 0:
            raise ValueError("record_count cannot be negative")
        digest = hashlib.sha256(artifact.content).hexdigest()
        artifact_blob = self._bucket.blob(self.artifact_path(digest))
        if artifact_blob.exists():
            if artifact_blob.download_as_bytes() != artifact.content:
                raise ValueError("content-addressed artifact bytes do not match their digest")
        else:
            artifact_blob.upload_from_string(
                artifact.content,
                content_type="application/vnd.kairo.theta-protobuf-decoded",
                if_generation_match=0,
            )
        metadata = CheckpointMetadata(
            unit_key=unit.unit_key,
            content_sha256=digest,
            format_version=artifact.format_version,
            serializer_version=unit.serializer_version,
            provider=unit.provider,
            endpoint=unit.endpoint,
            symbol=unit.symbol,
            session=unit.session.isoformat(),
            signal_at=None if unit.signal_at is None else _utc_timestamp(unit.signal_at),
            sealed_at=_utc_timestamp(sealed_at),
            record_count=record_count,
        )
        self._bucket.blob(self.checkpoint_path(unit)).upload_from_string(
            metadata.canonical_bytes(),
            content_type="application/json",
            if_generation_match=0,
        )
        return metadata

    @staticmethod
    def _validate_metadata(unit: AcquisitionUnit, metadata: CheckpointMetadata) -> None:
        expected_signal = None if unit.signal_at is None else _utc_timestamp(unit.signal_at)
        if (
            metadata.unit_key != unit.unit_key
            or metadata.provider != unit.provider
            or metadata.endpoint != unit.endpoint
            or metadata.symbol != unit.symbol
            or metadata.session != unit.session.isoformat()
            or metadata.format_version != THETA_DECODED_FORMAT_VERSION
            or metadata.serializer_version != unit.serializer_version
            or metadata.signal_at != expected_signal
            or metadata.record_count < 0
        ):
            raise ValueError("checkpoint metadata does not match the acquisition unit")
        try:
            sealed_at = datetime.strptime(metadata.sealed_at, "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError as error:
            raise ValueError("checkpoint sealed_at is not canonical UTC") from error
        if sealed_at.tzinfo is not None:
            raise ValueError("checkpoint sealed_at encoding is invalid")
        _validate_digest(metadata.content_sha256)


def acquire_or_resume(
    store: GCSCheckpointStore,
    unit: AcquisitionUnit,
    provider_fetch: Callable[[], ArtifactT],
    record_count: Callable[[ArtifactT], int],
    *,
    sealed_at: datetime,
) -> tuple[CheckpointMetadata, bool]:
    """Return checkpoint metadata and whether a provider fetch was performed."""
    if store.checkpoint_exists(unit):
        return store.load(unit), False
    artifact = provider_fetch()
    return store.seal(
        unit,
        artifact,
        record_count=record_count(artifact),
        sealed_at=sealed_at,
    ), True


def emit_structured_log(
    *,
    severity: str,
    event: str,
    symbol: str,
    session: date,
    unit_key: str,
    content_sha256: str,
    records: int,
) -> None:
    print(
        canonical_json_bytes(
            {
                "severity": severity,
                "event": event,
                "symbol": symbol,
                "session": session.isoformat(),
                "unit_key": unit_key,
                "content_sha256": content_sha256,
                "records": records,
            }
        ).decode("utf-8"),
        flush=True,
    )


def _validate_digest(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("SHA-256 must be 64 lowercase hexadecimal characters")


def _safe_component(value: str, label: str) -> str:
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    if not value or any(character not in allowed for character in value):
        raise ValueError(f"{label} is not safe for a checkpoint path")
    return value
