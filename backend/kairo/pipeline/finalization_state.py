"""Durable, fail-closed state primitives for Q1 finalization."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4


DATASET = "q1-2024"
BUCKET = "kairo-market-artifacts-507516"
PREFIX = "finalization/q1_2024"
RECEIPT_VERSION = "KAIRO-Q1-FINALIZATION-RECEIPT-v1"
INDEX_SCHEMA_VERSION = "KAIRO-Q1-OPTION-INDEX-v1"
NORMALIZATION_STAGE_VERSION = "KAIRO-Q1-NORMALIZATION-v1"
AUTHORITY_STAGE_VERSION = "KAIRO-Q1-AUTHORITY-v1"
MANIFEST_STAGE_VERSION = "KAIRO-Q1-MANIFEST-SEAL-v1"


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_file(path: str | Path, chunk_size: int = 8 * 1024 * 1024) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


@dataclass(frozen=True)
class ObjectIdentity:
    uri: str
    generation: str
    metageneration: str
    byte_count: int
    sha256: str | None = None


@dataclass(frozen=True)
class Receipt:
    receipt_version: str
    dataset: str
    stage: int
    stage_version: str
    predecessor_sha256: str | None
    inputs: tuple[dict[str, Any], ...]
    outputs: tuple[dict[str, Any], ...]
    facts: dict[str, Any]

    def canonical_bytes(self) -> bytes:
        return canonical_bytes(asdict(self))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def parse(cls, content: bytes) -> "Receipt":
        try:
            value = json.loads(content)
            receipt = cls(
                receipt_version=value["receipt_version"],
                dataset=value["dataset"],
                stage=value["stage"],
                stage_version=value["stage_version"],
                predecessor_sha256=value.get("predecessor_sha256"),
                inputs=tuple(value["inputs"]),
                outputs=tuple(value["outputs"]),
                facts=dict(value["facts"]),
            )
        except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("finalization receipt is invalid") from error
        if receipt.canonical_bytes() != content:
            raise ValueError("finalization receipt is not canonical")
        if receipt.receipt_version != RECEIPT_VERSION or receipt.dataset != DATASET:
            raise ValueError("finalization receipt contract mismatch")
        return receipt


class DurableObjectStore(Protocol):
    def stat(self, object_name: str) -> ObjectIdentity | None: ...
    def download(self, object_name: str, destination: Path) -> ObjectIdentity: ...
    def upload_immutable(
        self, object_name: str, source: Path, *, content_type: str, sha256: str, byte_count: int
    ) -> ObjectIdentity: ...
    def read_bytes(self, object_name: str) -> bytes: ...
    def seal_bytes(self, object_name: str, content: bytes, *, content_type: str) -> ObjectIdentity: ...


class GCSDurableObjectStore:
    """Generation-bound immutable objects; receipts are the multi-object commit point."""

    def __init__(self, bucket_name: str = BUCKET) -> None:
        from google.cloud import storage

        self.bucket_name = bucket_name.removeprefix("gs://")
        self.bucket = storage.Client().bucket(self.bucket_name)

    def stat(self, object_name: str) -> ObjectIdentity | None:
        blob = self.bucket.blob(object_name)
        if not blob.exists():
            return None
        blob.reload()
        metadata = blob.metadata or {}
        return ObjectIdentity(
            uri=f"gs://{self.bucket_name}/{object_name}",
            generation=str(blob.generation),
            metageneration=str(blob.metageneration),
            byte_count=int(blob.size),
            sha256=metadata.get("kairo-sha256"),
        )

    def download(self, object_name: str, destination: Path) -> ObjectIdentity:
        before = self.stat(object_name)
        if before is None:
            raise FileNotFoundError(f"required GCS object is absent: {object_name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{uuid4().hex}.tmp")
        blob = self.bucket.blob(object_name, generation=int(before.generation))
        try:
            with temporary.open("xb") as stream:
                blob.download_to_file(stream, if_generation_match=int(before.generation))
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        after = self.stat(object_name)
        if after is None or (after.generation, after.metageneration) != (
            before.generation,
            before.metageneration,
        ):
            raise ValueError("GCS object identity changed during download")
        return after

    def upload_immutable(
        self, object_name: str, source: Path, *, content_type: str, sha256: str, byte_count: int
    ) -> ObjectIdentity:
        actual_hash, actual_size = sha256_file(source)
        if (actual_hash, actual_size) != (sha256, byte_count):
            raise ValueError("durable output differs from supplied identity")
        existing = self.stat(object_name)
        if existing is not None:
            if existing.byte_count != byte_count or existing.sha256 != sha256:
                raise ValueError(f"immutable GCS output conflicts: {object_name}")
            return existing
        blob = self.bucket.blob(object_name)
        blob.metadata = {"kairo-sha256": sha256}
        blob.upload_from_filename(
            str(source), content_type=content_type, if_generation_match=0, checksum="crc32c"
        )
        result = self.stat(object_name)
        if result is None or result.byte_count != byte_count or result.sha256 != sha256:
            raise ValueError("uploaded GCS output failed identity validation")
        return result

    def read_bytes(self, object_name: str) -> bytes:
        identity = self.stat(object_name)
        if identity is None:
            raise FileNotFoundError(object_name)
        return self.bucket.blob(object_name, generation=int(identity.generation)).download_as_bytes(
            if_generation_match=int(identity.generation)
        )

    def seal_bytes(self, object_name: str, content: bytes, *, content_type: str) -> ObjectIdentity:
        digest = hashlib.sha256(content).hexdigest()
        existing = self.stat(object_name)
        if existing is not None:
            if self.read_bytes(object_name) != content:
                raise ValueError(f"durable receipt conflicts: {object_name}")
            if existing.sha256 != digest:
                blob = self.bucket.blob(object_name, generation=int(existing.generation))
                blob.reload()
                blob.metadata = {**(blob.metadata or {}), "kairo-sha256": digest}
                blob.patch(if_metageneration_match=int(existing.metageneration))
                existing = self.stat(object_name)
                if existing is None or existing.sha256 != digest:
                    raise ValueError("existing durable object could not be identity-sealed")
            return existing
        blob = self.bucket.blob(object_name)
        blob.metadata = {"kairo-sha256": digest}
        blob.upload_from_string(content, content_type=content_type, if_generation_match=0)
        result = self.stat(object_name)
        if result is None or result.sha256 != digest or result.byte_count != len(content):
            raise ValueError("sealed receipt failed identity validation")
        return result


def receipt_path(stage: int) -> str:
    if stage not in (1, 2, 3, 4):
        raise ValueError("finalization stage must be 1 through 4")
    return f"{PREFIX}/receipts/stage-{stage}.json"


def load_receipt(store: DurableObjectStore, stage: int) -> Receipt | None:
    path = receipt_path(stage)
    identity = store.stat(path)
    if identity is None:
        return None
    receipt = Receipt.parse(store.read_bytes(path))
    if receipt.stage != stage:
        raise ValueError("finalization receipt stage mismatch")
    return receipt


def seal_receipt(store: DurableObjectStore, receipt: Receipt) -> ObjectIdentity:
    return store.seal_bytes(
        receipt_path(receipt.stage),
        receipt.canonical_bytes(),
        content_type="application/vnd.kairo.finalization-receipt+json",
    )


def restore_verified_object(
    store: DurableObjectStore, descriptor: dict[str, Any], destination: Path
) -> None:
    object_name = descriptor["object_name"]
    expected = ObjectIdentity(**descriptor["identity"])
    actual = store.stat(object_name)
    if actual != expected:
        raise ValueError(f"durable object identity contradicts receipt: {object_name}")
    store.download(object_name, destination)
    digest, size = sha256_file(destination)
    if (digest, size) != (expected.sha256, expected.byte_count):
        raise ValueError(f"restored durable object failed SHA-256: {object_name}")
