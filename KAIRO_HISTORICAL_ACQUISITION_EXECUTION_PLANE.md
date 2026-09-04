# KAIRO SPRINT SPECIFICATION: HISTORICAL ACQUISITION EXECUTION PLANE (V3 - FINAL)

## 1. HARD SYSTEM FACTS & PARAMETERS
- Python Target: Python >= 3.12 (Base image: `python:3.12-slim`)
- Google Cloud Project: `kairo-research-507516`
- Target Region: `us-south1`
- Cloud SQL Instance Connection Name: `kairo-research-507516:us-south1:kairo-research-db`
- Migration Head: `0027` (Strict invariant: Zero new migrations, zero schema changes)
- CI Conformance Baseline: 549 tests passing in PostgreSQL 16 CI workflow
- Target Artifact Bucket: `gs://kairo-market-artifacts-507516`
- Cloud Run Job Identifier: `kairo-historical-ingestion`
- Secret Manager Resource Names:
  - `projects/kairo-research-507516/secrets/thetadata-api-key:latest`
  - `projects/kairo-research-507516/secrets/kairo-runtime-db-url:latest`

---

## 2. AUTHORIZED FILE POLICY & IMMUTABLE BOUNDARIES

### A. Pre-Existing File Lock
You are strictly forbidden from modifying, refactoring, reformatting, or deleting any pre-existing file in the repository, specifically including:
- `scripts/data/fetch_pilot_corpus.py`
- All files under `backend/engine/`
- All files under `backend/app/domain/`
- All files under `backend/alembic/`
- All pre-existing test files defining the 549 baseline

### B. Authorized File Creation List
You are permitted to create ONLY the following files (and any strictly necessary `__init__.py` files inside newly created directories):
- `Dockerfile` (at repository root)
- `.dockerignore` (at repository root)
- `backend/app/infrastructure/storage/gcs_checkpoint.py`
- `scripts/data/cloud_smoke_test.py`
- `backend/tests/test_cloud_execution_plane.py`
- `deploy/deploy_cloud_run_job.sh` (and/or `.ps1`)

**Stop Condition on Collision:** If any of the above paths already exist in the working tree, STOP immediately and report the collision rather than overwriting or merging.

---

## 3. TECHNICAL SPECIFICATIONS

### A. Database URL & Secret Manager Consumption
- `KAIRO_RUNTIME_DATABASE_URL` is supplied entirely by Secret Manager.
- Existing Kairo settings code (`backend/app/config.py`) consumes the environment value unchanged.
- Therefore, the secret value itself must be an authoritative, complete `postgresql+psycopg` SQLAlchemy URL configured to connect through the Cloud SQL Unix socket mounted at:
  `/cloudsql/kairo-research-507516:us-south1:kairo-research-db`
  *(e.g., `postgresql+psycopg://<user>:<password>@/kairo_research?host=/cloudsql/kairo-research-507516:us-south1:kairo-research-db`)*.
- Cody must not construct, synthesize, print, log, or commit credentials.
- Cloud Run must attach the instance with:
  `--set-cloudsql-instances=kairo-research-507516:us-south1:kairo-research-db`
- In the same project, secret bindings follow standard Cloud Run syntax:
  `--set-secrets THETADATA_API_KEY=thetadata-api-key:latest,KAIRO_RUNTIME_DATABASE_URL=kairo-runtime-db-url:latest`
- **Fail-Closed Contradiction Gate:** If the existing application cannot consume a Unix-socket-compatible `KAIRO_RUNTIME_DATABASE_URL` without modifying a frozen file (such as `app/config.py`), STOP immediately and report the contradiction.

### B. Content-Addressed GCS Checkpoint Architecture
- Provider Evidence Integrity: Store raw provider bytes in their native `THETA_PROTOBUF_DECODED-v1` format. Never convert or serialize raw evidence directly to Parquet.
- Content-Addressed Path Layout:
  `gs://<bucket>/artifacts/theta/sha256/{content_sha256[:2]}/{content_sha256}.bin`
- Deterministic Checkpoint Identity (`unit_key`):
  ```python
  unit_key = hashlib.sha256(
      f"{provider}|{endpoint}|{symbol}|{session}|{signal_at}|"
      f"{sorted(target_dtes)}|{strikes_each_side}|"
      f"{serializer_version}|{acquisition_policy_version}".encode("utf-8")
  ).hexdigest()

  Checkpoint Path:
gs://<bucket>/checkpoints/{symbol}/{session}/{unit_key}.json

Checkpoint Metadata Schema:

JSON
{
  "unit_key": "...",
  "content_sha256": "...",
  "format_version": "THETA_PROTOBUF_DECODED-v1",
  "serializer_version": "...",
  "provider": "THETA_DATA",
  "endpoint": "option_history_quote",
  "symbol": "TQQQ",
  "session": "2024-01-10",
  "signal_at": "2024-01-10T14:32:00Z",
  "sealed_at": "2026-09-04T12:00:00Z",
  "record_count": 42
}
Use if-generation-match=0 (create-if-absent) on checkpoint writes to enforce atomic, race-free sealing.

C. Resume & Idempotency Pipeline
Check GCS for checkpoints/{symbol}/{session}/{unit_key}.json.

If present, skip outbound network queries entirely.

If absent, execute provider query, verify SHA-256 integrity, persist .bin to GCS, write checkpoint JSON, emit structured log event, and proceed.

D. Structured Telemetry (Cloud Logging)
All runtime progress must emit single-line, valid JSON to stdout:

JSON
{
  "severity": "INFO",
  "event": "ACQUISITION_UNIT_SEALED",
  "symbol": "TQQQ",
  "session": "2024-01-10",
  "unit_key": "...",
  "content_sha256": "...",
  "records": 42
}
E. Smoke-Runner Safety Boundaries (scripts/data/cloud_smoke_test.py)
The smoke runner is a standalone, isolated diagnostic harness for verifying container wiring.

Hardcoded Constraint: Hard-capped at exactly 1 symbol and 1 trading session.

Gating Flag: Requires an explicit --authorize-cloud-smoke-test CLI flag and dedicated environment variable check (KAIRO_CLOUD_SMOKE_AUTHORIZED=1).

Isolation: Must NOT import, alter, or loosen scripts/data/fetch_pilot_corpus.py or its frozen 30–61 session validation logic.

Scope: Validates Secret Manager ingestion, Cloud SQL reachability via Unix socket, GCS checkpoint creation, and a single bounded underlying bar query.

4. CONFORMANCE TEST REQUIREMENTS
Implement in backend/tests/test_cloud_execution_plane.py (using mocks; no live cloud resources or paid Theta requests during tests):

test_dockerfile_python_version_and_paths: Assert base image is python:3.12-slim and build context accommodates backend/ and scripts/.

test_unit_key_sensitivity: Prove changing any parameter alters the resulting unit_key.

test_gcs_checkpoint_atomic_seal: Validate payload byte preservation and checkpoint payload structure.

test_resume_skips_provider_fetch: Prove sealed checkpoints bypass provider fetches with zero outbound network calls.

test_smoke_runner_rejects_exceeded_scope: Prove cloud_smoke_test.py fails closed if requested for >1 day or >1 symbol.

test_structured_log_emission_valid_json: Validate log format conformance to Google Cloud Logging schemas.

5. ACCEPTANCE CRITERIA
Local Verification:

git diff --name-only backend/engine/ backend/app/domain/ backend/alembic/ scripts/data/fetch_pilot_corpus.py outputs nothing.

Dockerfile builds cleanly from root context (docker build -t kairo-engine:test .).

All new execution-plane tests pass locally.

CI Verification Authority:

PostgreSQL 16 GitHub Actions workflow passes with >= 549 + new tests.

Migration head confirmed at 0027.

Execution Stop:

Do NOT execute live multi-year pulls.

Do NOT run Attempt #4.

STOP immediately after passing CI.