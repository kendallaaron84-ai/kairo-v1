import hashlib
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

os.environ.setdefault(
    "KAIRO_RUNTIME_DATABASE_URL",
    "postgresql+psycopg://unused:unused@127.0.0.1:1/unused",
)

from app.api.dependencies import get_artifact_storage  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.session import get_db  # noqa: E402
from engine.data.corpus_qualifier import (  # noqa: E402
    CorpusQualificationManifest,
    PilotWindow,
    QualificationMetrics,
    QualificationStatus,
)
from engine.data.option_enrollment import CanonicalResolutionAccounting  # noqa: E402
from engine.intelligence.storage_driver import GCSReadOnlyArtifactStorage  # noqa: E402


UTC = timezone.utc


class StubArtifactStorage:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.reads: list[str] = []

    def read_bytes(self, storage_uri: str) -> bytes:
        self.reads.append(storage_uri)
        return self.content


class ScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class RowsResult:
    def __init__(self, rows=()) -> None:
        self.rows = tuple(rows)

    def all(self):
        return list(self.rows)


class StubSession:
    def __init__(self, *, scalar_values=(), rows=(), version="0027") -> None:
        self.scalar_values = list(scalar_values)
        self.rows = tuple(rows)
        self.version = version

    def scalar(self, _statement):
        return self.scalar_values.pop(0)

    def execute(self, statement):
        if getattr(statement, "text", None) == "SELECT version_num FROM alembic_version":
            return ScalarResult(self.version)
        return RowsResult(self.rows)

    def scalars(self, _statement):
        return iter(())


def _manifest(dataset_hash: str) -> CorpusQualificationManifest:
    accounting = CanonicalResolutionAccounting(
        discovered_contracts_count=1,
        resolved_existing_contracts_count=1,
        newly_enrolled_contracts_count=0,
        resolved_contracts_count=1,
        rejected_contracts_count=0,
    )
    return CorpusQualificationManifest(
        qualification_manifest_id=uuid4(),
        qualification_manifest_sha256="a" * 64,
        qualification_policy_version="CORPUS-QUALIFICATION-v1",
        provider_code="THETA_DATA",
        pilot_window=PilotWindow(
            start_session=date(2024, 1, 2),
            end_session=date(2024, 3, 28),
            total_calendar_sessions=61,
            rth_expected_minutes=23790,
        ),
        metrics=QualificationMetrics(
            underlying_bar_completeness_pct=Decimal("100.00"),
            underlying_status=QualificationStatus.PASS,
            strategy_signal_count=1,
            decision_point_complete_evidence_count=1,
            decision_point_evidence_pct=Decimal("100.00"),
            decision_evidence_status=QualificationStatus.PASS,
            causal_timestamp_violations_count=0,
            causal_status=QualificationStatus.PASS,
            canonical_contract_resolution_pct=Decimal("100.00"),
            resolution_status=QualificationStatus.PASS,
            resolution_accounting=accounting,
            assigned_fidelity_tier="TIER_1_QUOTE_DEPTH",
            fidelity_status=QualificationStatus.PASS,
        ),
        overall_qualification_verdict=QualificationStatus.PASS,
        raw_artifacts_manifest_sha256="b" * 64,
        normalized_dataset_manifest_sha256=dataset_hash,
    )


def _qualification_objects():
    dataset_hash = "c" * 64
    manifest = _manifest(dataset_hash)
    content = manifest.canonical_bytes()
    artifact = SimpleNamespace(
        artifact_id=uuid4(),
        artifact_role="NORMALIZED_RESEARCH_STREAM",
        content_sha256=hashlib.sha256(content).hexdigest(),
        mime_type="application/vnd.kairo.corpus-qualification+json",
        byte_size=len(content),
        storage_uri="gs://kairo-test/manifests/theta_q1_2024_manifest.json",
        created_at=datetime(2024, 3, 29, tzinfo=UTC),
    )
    dataset = SimpleNamespace(
        dataset_id=uuid4(),
        dataset_name="theta-q1-2024",
        provider_name="THETA_DATA",
        dataset_manifest_sha256=dataset_hash,
        ingested_at=datetime(2024, 3, 29, tzinfo=UTC),
    )
    return content, artifact, dataset


def _client(monkeypatch, session: StubSession, content: bytes):
    monkeypatch.setenv(
        "KAIRO_QUALIFICATION_MANIFEST_GCS_URI",
        "gs://kairo-test/manifests/theta_q1_2024_manifest.json",
    )
    monkeypatch.setenv("KAIRO_ARTIFACT_GCS_BUCKET", "kairo-test")
    monkeypatch.setenv("KAIRO_CAPITAL_API_TOKEN", "test-capital-token")
    get_settings.cache_clear()

    from app.main import app

    storage = StubArtifactStorage(content)

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_artifact_storage] = lambda: storage
    return TestClient(app), storage


def _cleanup_app() -> None:
    from app.main import app

    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_system_status_exposes_migration_and_dataset_health(monkeypatch) -> None:
    content, artifact, dataset = _qualification_objects()
    client, _ = _client(
        monkeypatch,
        StubSession(scalar_values=(dataset, artifact.artifact_id)),
        content,
    )
    try:
        response = client.get("/api/v1/system/status")
        assert response.status_code == 200
        body = response.json()
        assert body["overall_status"] == "HEALTHY"
        assert body["database"]["migration_head"] == "0027"
        assert body["research"]["latest_dataset_id"] == str(dataset.dataset_id)
        assert body["research"]["qualification_manifest_available"] is True
    finally:
        _cleanup_app()


def test_system_status_reports_degraded_when_database_is_unavailable(monkeypatch) -> None:
    class UnavailableSession(StubSession):
        def execute(self, _statement):
            raise OperationalError("SELECT 1", {}, RuntimeError("offline"))

    client, _ = _client(monkeypatch, UnavailableSession(), b"unused")
    try:
        response = client.get("/api/v1/system/status")
        assert response.status_code == 200
        body = response.json()
        assert body["overall_status"] == "DEGRADED"
        assert body["database"] == {"status": "DEGRADED", "migration_head": None}
        assert body["research"]["qualification_manifest_available"] is False
    finally:
        _cleanup_app()


def test_latest_qualification_is_hash_verified_and_dataset_bound(monkeypatch) -> None:
    content, artifact, dataset = _qualification_objects()
    client, storage = _client(
        monkeypatch, StubSession(scalar_values=(artifact, dataset)), content
    )
    try:
        response = client.get("/api/v1/research/qualification/latest")
        assert response.status_code == 200
        body = response.json()
        assert body["dataset_id"] == str(dataset.dataset_id)
        assert body["artifact"]["content_sha256"] == artifact.content_sha256
        assert body["qualification"]["normalized_dataset_manifest_sha256"] == "c" * 64
        assert storage.reads == ["gs://kairo-test/manifests/theta_q1_2024_manifest.json"]
    finally:
        _cleanup_app()


def test_q1_matrix_is_bound_to_exact_qualification_window(monkeypatch) -> None:
    content, artifact, dataset = _qualification_objects()
    client, _ = _client(
        monkeypatch, StubSession(scalar_values=(artifact, dataset)), content
    )
    try:
        response = client.get("/api/v1/research/matrix/q1-2024")
        assert response.status_code == 200
        assert response.json() == {
            "schema_version": "kairo.research-matrix.v1",
            "dataset_id": str(dataset.dataset_id),
            "dataset_name": "theta-q1-2024",
            "provider_name": "THETA_DATA",
            "start_session": "2024-01-02",
            "end_session": "2024-03-28",
            "symbols": ["TQQQ", "SQQQ"],
            "streams": [],
        }
    finally:
        _cleanup_app()


def test_capital_ledgers_fail_closed_without_valid_internal_token(monkeypatch) -> None:
    client, _ = _client(monkeypatch, StubSession(), b"unused")
    try:
        assert client.get("/api/v1/capital/ledgers").status_code == 401
        assert client.get(
            "/api/v1/capital/ledgers",
            headers={"Authorization": "Bearer wrong"},
        ).status_code == 401
        authorized = client.get(
            "/api/v1/capital/ledgers",
            headers={"Authorization": "Bearer test-capital-token"},
        )
        assert authorized.status_code == 200
        assert authorized.json()["items"] == []
    finally:
        _cleanup_app()


def test_capital_ledgers_fail_closed_when_authentication_is_unconfigured(
    monkeypatch,
) -> None:
    client, _ = _client(monkeypatch, StubSession(), b"unused")
    monkeypatch.delenv("KAIRO_CAPITAL_API_TOKEN")
    get_settings.cache_clear()
    try:
        assert client.get("/api/v1/capital/ledgers").status_code == 503
    finally:
        _cleanup_app()


def test_gcs_reader_uses_exact_object_and_has_no_write_or_list_surface() -> None:
    class Blob:
        def download_as_bytes(self, **kwargs):
            assert kwargs == {"checksum": "auto", "retry": None, "timeout": 60}
            return b"sealed-manifest"

    class Bucket:
        def blob(self, object_name):
            assert object_name == "manifests/theta_q1_2024_manifest.json"
            return Blob()

    class Client:
        def bucket(self, bucket_name):
            assert bucket_name == "kairo-test"
            return Bucket()

    storage = GCSReadOnlyArtifactStorage(expected_bucket="kairo-test", client=Client())
    assert storage.read_bytes(
        "gs://kairo-test/manifests/theta_q1_2024_manifest.json"
    ) == b"sealed-manifest"
    assert not hasattr(storage, "write_bytes")
    assert not hasattr(storage, "list")


def test_gcs_reader_rejects_active_attempt_staging_without_access() -> None:
    class NoAccessClient:
        def bucket(self, _bucket_name):
            raise AssertionError("staging rejection must occur before GCS access")

    storage = GCSReadOnlyArtifactStorage(
        expected_bucket="kairo-test", client=NoAccessClient()
    )
    with pytest.raises(ValueError, match="staging"):
        storage.read_bytes(
            "gs://kairo-test/.attempt-4-staging-v1/checkpoints/unit.json"
        )
