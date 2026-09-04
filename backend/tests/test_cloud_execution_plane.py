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


def load_pilot_module():
    path = ROOT / "scripts" / "data" / "cloud_pilot_qualification.py"
    spec = importlib.util.spec_from_file_location("kairo_cloud_pilot_qualification", path)
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


def test_cloud_run_deployment_uses_discrete_pilot_argument_array_and_gcs_mount():
    deployment = (ROOT / "deploy" / "deploy_cloud_run_job.sh").read_text(
        encoding="utf-8"
    )
    match = re.search(r'^PILOT_ARGS="\^\|\^([^"]+)"$', deployment, flags=re.MULTILINE)
    assert match is not None
    assert ["python", *match.group(1).split("|")] == [
        "python",
        "scripts/data/cloud_pilot_qualification.py",
        "--provider",
        "theta",
        "--start",
        "2024-01-02",
        "--end",
        "2024-03-28",
        "--symbols",
        "TQQQ,SQQQ",
        "--qualify",
        "--bucket",
        "kairo-market-artifacts-507516",
        "--manifest-object",
        "manifests/theta_q1_2024_manifest.json",
        "--storage-root",
        "/mnt/kairo-market-artifacts/historical-market",
        "--authorize-paid-theta-history",
    ]
    assert '--command=python' in deployment
    assert '--args="${PILOT_ARGS}"' in deployment
    assert "KAIRO_CLOUD_PILOT_AUTHORIZED=1" in deployment
    assert "type=cloud-storage,bucket=kairo-market-artifacts-507516" in deployment
    assert "mount-path=/mnt/kairo-market-artifacts" in deployment
    assert deployment.count("--max-retries=0") == 1
    assert "--execute-now" not in deployment


def test_gcloud_source_archive_preserves_smoke_runner():
    ignore_lines = {
        line.strip()
        for line in (ROOT / ".gcloudignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "#!include:.gitignore" not in ignore_lines
    assert "data" not in ignore_lines
    assert "data/" not in ignore_lines
    assert "/data" in ignore_lines
    assert "scripts" not in ignore_lines
    assert "scripts/data" not in ignore_lines
    assert "scripts/data/cloud_smoke_test.py" not in ignore_lines
    assert "scripts/data/cloud_pilot_qualification.py" not in ignore_lines


def test_verified_artifact_materialization_detects_corruption():
    checkpoint_store, client = store()
    payload = Artifact(b"verified-provider-bytes")
    metadata = checkpoint_store.seal(
        unit(),
        payload,
        record_count=3,
        sealed_at=datetime(2026, 9, 4, 12, tzinfo=timezone.utc),
    )
    assert checkpoint_store.read_artifact(metadata) == payload.content
    artifact_path = checkpoint_store.artifact_path(metadata.content_sha256)
    client.value.blobs[artifact_path].data = b"corrupted"
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        checkpoint_store.read_artifact(metadata)


def test_underlying_and_option_units_use_frozen_resume_granularity():
    pilot = load_pilot_module()
    underlying = pilot.underlying_unit("TQQQ", date(2024, 1, 10))
    assert underlying.endpoint == "stock_history_ohlc"
    assert underlying.signal_at is None
    assert underlying.target_dtes == ()
    assert underlying.strikes_each_side == 0

    signal_at = datetime(2024, 1, 10, 15, 32, tzinfo=timezone.utc)
    option = pilot.option_unit("TQQQ", signal_at)
    assert option.endpoint == "OPTION_STRIKE_NEIGHBORHOOD"
    assert option.signal_at == signal_at
    assert option.target_dtes == (0, 1, 7, 14, 30)
    assert option.strikes_each_side == 10


def test_cloud_pilot_requires_dual_authorization_and_stable_storage_mount():
    pilot = load_pilot_module()
    calendar = Mock()
    calendar.sessions.return_value = tuple((date(2024, 1, day), None, None) for day in range(1, 31))
    args = argparse.Namespace(
        symbols="TQQQ,SQQQ",
        start=date(2024, 1, 2),
        end=date(2024, 3, 28),
        authorize_paid_theta_history=True,
        storage_root=Path("/mnt/kairo-market-artifacts/historical-market"),
        manifest_object="manifests/theta_q1_2024_manifest.json",
    )
    assert pilot.validate_scope(args, {"KAIRO_CLOUD_PILOT_AUTHORIZED": "1"}, calendar) == (
        "TQQQ",
        "SQQQ",
    )
    with pytest.raises(PermissionError, match="KAIRO_CLOUD_PILOT_AUTHORIZED"):
        pilot.validate_scope(args, {}, calendar)
    with pytest.raises(ValueError, match="storage root"):
        pilot.validate_scope(
            argparse.Namespace(**{**vars(args), "storage_root": Path("/tmp")}),
            {"KAIRO_CLOUD_PILOT_AUTHORIZED": "1"},
            calendar,
        )
    for start, end in (
        (date(2024, 1, 3), date(2024, 3, 28)),
        (date(2024, 1, 2), date(2024, 3, 27)),
    ):
        with pytest.raises(ValueError, match="Attempt #4 requires exactly"):
            pilot.validate_scope(
                argparse.Namespace(**{**vars(args), "start": start, "end": end}),
                {"KAIRO_CLOUD_PILOT_AUTHORIZED": "1"},
                calendar,
            )


def test_partial_resume_materializes_sealed_bytes_without_provider_fetch():
    pilot = load_pilot_module()
    checkpoint_store, _ = store()
    serializer = ThetaDecodedArtifactSerializer()
    content = serializer.serialize(
        [], acquisition_request={"symbol": "TQQQ", "session": date(2024, 1, 10)}
    )
    acquisition = pilot.underlying_unit("TQQQ", date(2024, 1, 10))
    checkpoint_store.seal(
        acquisition,
        Artifact(content),
        record_count=0,
        sealed_at=datetime(2026, 9, 4, 12, tzinfo=timezone.utc),
    )
    provider_fetch = Mock(side_effect=AssertionError("sealed unit must not refetch"))
    materialized = pilot.acquire_unit(checkpoint_store, acquisition, provider_fetch)
    assert materialized.content == content
    provider_fetch.assert_not_called()


def test_qualification_manifest_is_sealed_only_after_persistence_succeeds():
    pilot = load_pilot_module()
    events = []
    manifest = Mock()
    manifest.canonical_bytes.return_value = b'{"qualification":"PASS"}'
    checkpoint_store = Mock()

    def persist():
        events.append("DATABASE_COMMITTED")
        return manifest

    checkpoint_store.seal_manifest.side_effect = lambda *_: events.append("GCS_SEALED")
    assert pilot.persist_then_seal_manifest(
        persist,
        checkpoint_store,
        "manifests/theta_q1_2024_manifest.json",
    ) is manifest
    assert events == ["DATABASE_COMMITTED", "GCS_SEALED"]

    checkpoint_store.reset_mock()
    with pytest.raises(RuntimeError, match="rollback"):
        pilot.persist_then_seal_manifest(
            Mock(side_effect=RuntimeError("database rollback")),
            checkpoint_store,
            "manifests/theta_q1_2024_manifest.json",
        )
    checkpoint_store.seal_manifest.assert_not_called()


def test_manifest_seal_is_create_only_idempotent_and_conflict_closed():
    checkpoint_store, client = store()
    object_name = "manifests/theta_q1_2024_manifest.json"
    content = b'{"qualification":"PASS"}'
    assert checkpoint_store.seal_manifest(object_name, content) is True
    blob = client.value.blobs[object_name]
    assert blob.uploads == [
        {
            "content_type": "application/vnd.kairo.corpus-qualification+json",
            "if_generation_match": 0,
        }
    ]
    assert checkpoint_store.seal_manifest(object_name, content) is False
    with pytest.raises(ValueError, match="conflict"):
        checkpoint_store.seal_manifest(object_name, b'{"qualification":"FAIL"}')


def test_reconstructed_aggregate_is_invariant_to_component_arrival_order():
    pilot = load_pilot_module()
    serializer = ThetaDecodedArtifactSerializer()

    def component(session_date: date):
        content = serializer.serialize(
            [
                pilot.DecodedThetaSection(
                    endpoint="stock_history_ohlc",
                    parameters={"symbol": "TQQQ", "session": session_date},
                    dataframe=[
                        {
                            "timestamp": datetime.combine(
                                session_date,
                                datetime.min.time(),
                                tzinfo=timezone.utc,
                            ),
                            "close": 50,
                        }
                    ],
                )
            ],
            acquisition_request={"symbol": "TQQQ", "session": session_date},
        )
        return pilot.RawProviderArtifact(
            provider_code="THETA_DATA",
            request_kind=pilot.ThetaDataProviderAdapter.BAR_REQUEST_KIND,
            content=content,
            mime_type=serializer.MIME_TYPE,
            request_parameters=(("session", session_date.isoformat()),),
            artifact_type=pilot.ProviderArtifactType.DECODED_PROVIDER_ARTIFACT,
            format_version=serializer.FORMAT_VERSION,
            serializer_version=serializer.SERIALIZER_VERSION,
        )

    components = (component(date(2024, 1, 2)), component(date(2024, 1, 3)))
    forward = pilot.aggregate_artifacts(
        components,
        request_kind=pilot.ThetaDataProviderAdapter.BAR_REQUEST_KIND,
        symbol="TQQQ",
    )
    reversed_result = pilot.aggregate_artifacts(
        reversed(components),
        request_kind=pilot.ThetaDataProviderAdapter.BAR_REQUEST_KIND,
        symbol="TQQQ",
    )
    assert forward.content == reversed_result.content
    assert forward.content_sha256 == reversed_result.content_sha256


def test_cloud_pilot_does_not_reference_dotenv_or_frozen_direct_acquisition_helpers():
    source = (ROOT / "scripts" / "data" / "cloud_pilot_qualification.py").read_text(
        encoding="utf-8"
    )
    assert "from_local_environment" not in source
    assert "dotenv" not in source
    assert "acquire_underlying_evidence" not in source
    assert "acquire_option_evidence" not in source
