import argparse
import hashlib
import importlib.util
import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.infrastructure.storage.gcs_checkpoint import (
    AcquisitionUnit,
    GCSCheckpointStore,
    acquire_or_resume,
    emit_structured_log,
)
from engine.data.theta_v3 import ThetaDecodedArtifactSerializer


ROOT = Path(__file__).resolve().parents[2]
SERIALIZER_VERSION = ThetaDecodedArtifactSerializer.SERIALIZER_VERSION


class FakeBlob:
    def __init__(self) -> None:
        self.data: bytes | None = None
        self.uploads: list[dict[str, object]] = []

    def exists(self) -> bool:
        return self.data is not None

    def download_as_bytes(self) -> bytes:
        if self.data is None:
            raise FileNotFoundError
        return self.data

    def upload_from_string(
        self,
        data: bytes,
        *,
        content_type: str,
        if_generation_match: int,
    ) -> None:
        if self.data is not None and if_generation_match == 0:
            raise RuntimeError("precondition failed")
        self.data = data
        self.uploads.append(
            {
                "content_type": content_type,
                "if_generation_match": if_generation_match,
            }
        )


class FakeBucket:
    def __init__(self) -> None:
        self.blobs: dict[str, FakeBlob] = {}

    def blob(self, name: str) -> FakeBlob:
        return self.blobs.setdefault(name, FakeBlob())


class FakeClient:
    def __init__(self) -> None:
        self.value = FakeBucket()

    def bucket(self, bucket_name: str) -> FakeBucket:
        assert bucket_name == "kairo-market-artifacts-507516"
        return self.value


@dataclass(frozen=True)
class Artifact:
    content: bytes
    format_version: str = "THETA_PROTOBUF_DECODED-v1"
    serializer_version: str = SERIALIZER_VERSION


def unit() -> AcquisitionUnit:
    return AcquisitionUnit(
        provider="THETA_DATA",
        endpoint="option_history_quote",
        symbol="TQQQ",
        session=date(2024, 1, 10),
        signal_at=datetime(2024, 1, 10, 14, 32, tzinfo=timezone.utc),
        target_dtes=(0, 1, 7, 14, 30),
        strikes_each_side=10,
        serializer_version=SERIALIZER_VERSION,
        acquisition_policy_version="PILOT-v1",
    )


def store() -> tuple[GCSCheckpointStore, FakeClient]:
    client = FakeClient()
    return GCSCheckpointStore(client, "gs://kairo-market-artifacts-507516"), client


def load_smoke_module():
    path = ROOT / "scripts" / "data" / "cloud_smoke_test.py"
    spec = importlib.util.spec_from_file_location("kairo_cloud_smoke_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dockerfile_python_version_and_paths():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert dockerfile.startswith("FROM python:3.12-slim\n")
    assert "COPY backend/app ./backend/app" in dockerfile
    assert "COPY backend/engine ./backend/engine" in dockerfile
    assert "COPY scripts ./scripts" in dockerfile
    assert "PYTHONPATH=/app/backend" in dockerfile


def test_unit_key_sensitivity():
    baseline = unit()
    changes = (
        {"provider": "OTHER"},
        {"endpoint": "option_history_trade"},
        {"symbol": "SQQQ"},
        {"session": date(2024, 1, 11)},
        {"signal_at": datetime(2024, 1, 10, 14, 33, tzinfo=timezone.utc)},
        {"target_dtes": (0, 1, 7)},
        {"strikes_each_side": 9},
        {"serializer_version": "SERIALIZER-v2"},
        {"acquisition_policy_version": "PILOT-v2"},
    )
    keys = {replace(baseline, **change).unit_key for change in changes}
    assert baseline.unit_key not in keys
    assert len(keys) == len(changes)


def test_gcs_checkpoint_atomic_seal():
    checkpoint_store, client = store()
    payload = Artifact(b"byte-exact-theta-decoded-provider-artifact")
    metadata = checkpoint_store.seal(
        unit(),
        payload,
        record_count=42,
        sealed_at=datetime(2026, 9, 4, 12, tzinfo=timezone.utc),
    )

    digest = hashlib.sha256(payload.content).hexdigest()
    artifact_blob = client.value.blobs[
        f"artifacts/theta/sha256/{digest[:2]}/{digest}.bin"
    ]
    assert artifact_blob.data == payload.content
    assert artifact_blob.uploads == [
        {
            "content_type": "application/vnd.kairo.theta-protobuf-decoded",
            "if_generation_match": 0,
        }
    ]
    checkpoint_blob = client.value.blobs[checkpoint_store.checkpoint_path(unit())]
    assert checkpoint_blob.uploads[0]["if_generation_match"] == 0
    assert json.loads(checkpoint_blob.data) == {
        "content_sha256": digest,
        "endpoint": "option_history_quote",
        "format_version": "THETA_PROTOBUF_DECODED-v1",
        "provider": "THETA_DATA",
        "record_count": 42,
        "sealed_at": "2026-09-04T12:00:00.000000Z",
        "serializer_version": SERIALIZER_VERSION,
        "session": "2024-01-10",
        "signal_at": "2024-01-10T14:32:00.000000Z",
        "symbol": "TQQQ",
        "unit_key": unit().unit_key,
    }
    assert checkpoint_store.load(unit()) == metadata


def test_resume_skips_provider_fetch():
    checkpoint_store, _ = store()
    payload = Artifact(b"already-sealed")
    existing = checkpoint_store.seal(
        unit(),
        payload,
        record_count=1,
        sealed_at=datetime(2026, 9, 4, 12, tzinfo=timezone.utc),
    )
    provider_fetch = Mock(side_effect=AssertionError("provider must not be called"))
    record_count = Mock(side_effect=AssertionError("artifact must not be decoded"))

    actual, fetched = acquire_or_resume(
        checkpoint_store,
        unit(),
        provider_fetch,
        record_count,
        sealed_at=datetime(2026, 9, 4, 13, tzinfo=timezone.utc),
    )

    assert actual == existing
    assert fetched is False
    provider_fetch.assert_not_called()
    record_count.assert_not_called()


def test_smoke_runner_rejects_exceeded_scope():
    smoke = load_smoke_module()
    environment = {"KAIRO_CLOUD_SMOKE_AUTHORIZED": "1"}
    base = {
        "symbols": "TQQQ",
        "start": date(2024, 1, 2),
        "end": date(2024, 1, 2),
        "authorize_cloud_smoke_test": True,
    }
    with pytest.raises(ValueError, match="exactly one symbol"):
        smoke.validate_scope(argparse.Namespace(**{**base, "symbols": "TQQQ,SQQQ"}), environment)
    with pytest.raises(ValueError, match="exactly one trading session"):
        smoke.validate_scope(
            argparse.Namespace(**{**base, "end": date(2024, 1, 3)}), environment
        )


def test_structured_log_emission_valid_json(capsys):
    emit_structured_log(
        severity="INFO",
        event="ACQUISITION_UNIT_SEALED",
        symbol="TQQQ",
        session=date(2024, 1, 10),
        unit_key="a" * 64,
        content_sha256="b" * 64,
        records=42,
    )
    line = capsys.readouterr().out
    assert line.count("\n") == 1
    assert json.loads(line) == {
        "severity": "INFO",
        "event": "ACQUISITION_UNIT_SEALED",
        "symbol": "TQQQ",
        "session": "2024-01-10",
        "unit_key": "a" * 64,
        "content_sha256": "b" * 64,
        "records": 42,
    }


def test_cloud_run_deployment_uses_discrete_smoke_argument_array():
    deployment = (ROOT / "deploy" / "deploy_cloud_run_job.sh").read_text(
        encoding="utf-8"
    )
    match = re.search(r'^SMOKE_ARGS="([^"]+)"$', deployment, flags=re.MULTILINE)
    assert match is not None
    assert ["python", *match.group(1).split(",")] == [
        "python",
        "scripts/data/cloud_smoke_test.py",
        "--symbols",
        "TQQQ",
        "--start",
        "2024-01-02",
        "--end",
        "2024-01-02",
        "--authorize-cloud-smoke-test",
    ]
    assert '--command=python' in deployment
    assert '--args="${SMOKE_ARGS}"' in deployment
    assert "KAIRO_CLOUD_SMOKE_AUTHORIZED=1" in deployment
    assert "--execute-now" not in deployment
