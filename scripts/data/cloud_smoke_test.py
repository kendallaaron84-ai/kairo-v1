"""Explicitly authorized, one-symbol/one-session Cloud Run wiring smoke test."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sqlalchemy import create_engine, text


BACKEND_ROOT = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.infrastructure.storage.gcs_checkpoint import (  # noqa: E402
    AcquisitionUnit,
    GCSCheckpointStore,
    acquire_or_resume,
    emit_structured_log,
)
from engine.data.provider_adapter import ThetaDataProviderAdapter  # noqa: E402
from engine.data.theta_v3 import (  # noqa: E402
    ThetaDataV3ClientTransport,
    ThetaDecodedArtifactReader,
    ThetaDecodedArtifactSerializer,
)


CLOUD_SQL_SOCKET = "/cloudsql/kairo-research-507516:us-south1:kairo-research-db"
DEFAULT_BUCKET = "kairo-market-artifacts-507516"
ACQUISITION_POLICY_VERSION = "KAIRO-CLOUD-SMOKE-v1"


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--authorize-cloud-smoke-test", action="store_true")
    return parser.parse_args(argv)


def validate_scope(args: argparse.Namespace, environment: dict[str, str]) -> str:
    symbols = tuple(part.strip().upper() for part in args.symbols.split(",") if part.strip())
    if len(symbols) != 1:
        raise ValueError("cloud smoke test requires exactly one symbol")
    if args.start != args.end:
        raise ValueError("cloud smoke test requires exactly one trading session")
    if not args.authorize_cloud_smoke_test:
        raise PermissionError("--authorize-cloud-smoke-test is required")
    if environment.get("KAIRO_CLOUD_SMOKE_AUTHORIZED") != "1":
        raise PermissionError("KAIRO_CLOUD_SMOKE_AUTHORIZED=1 is required")
    return symbols[0]


def validate_runtime_database_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "postgresql+psycopg":
        raise ValueError("KAIRO_RUNTIME_DATABASE_URL must use postgresql+psycopg")
    if parse_qs(parsed.query).get("host") != [CLOUD_SQL_SOCKET]:
        raise ValueError("KAIRO_RUNTIME_DATABASE_URL must target the authorized Cloud SQL socket")


def verify_database_reachability(database_url: str) -> None:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()


def _record_count(artifact) -> int:
    decoded = ThetaDecodedArtifactReader().read_provider_artifact(artifact)
    return sum(len(section.records) for section in decoded.sections)


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    symbol = validate_scope(args, dict(os.environ))
    api_key = os.environ.get("THETADATA_API_KEY")
    database_url = os.environ.get("KAIRO_RUNTIME_DATABASE_URL")
    if not api_key or not database_url:
        raise RuntimeError("required Secret Manager environment injection is absent")
    validate_runtime_database_url(database_url)
    verify_database_reachability(database_url)

    unit = AcquisitionUnit(
        provider="THETA_DATA",
        endpoint="stock_history_ohlc",
        symbol=symbol,
        session=args.start,
        signal_at=None,
        target_dtes=(),
        strikes_each_side=0,
        serializer_version=ThetaDecodedArtifactSerializer.SERIALIZER_VERSION,
        acquisition_policy_version=ACQUISITION_POLICY_VERSION,
    )
    store = GCSCheckpointStore.from_default_credentials(args.bucket)

    def provider_fetch():
        from thetadata import ThetaClient

        client = ThetaClient(api_key=api_key, dataframe_type="polars")
        transport = ThetaDataV3ClientTransport(
            client,
            allow_paid_historical_retrieval=True,
        )
        return ThetaDataProviderAdapter(transport).fetch_equity_minute_bars(
            symbol=symbol,
            start_session=args.start,
            end_session=args.end,
        )

    metadata, fetched = acquire_or_resume(
        store,
        unit,
        provider_fetch,
        _record_count,
        sealed_at=datetime.now(timezone.utc),
    )
    emit_structured_log(
        severity="INFO",
        event="ACQUISITION_UNIT_SEALED" if fetched else "ACQUISITION_UNIT_RESUMED",
        symbol=symbol,
        session=args.start,
        unit_key=metadata.unit_key,
        content_sha256=metadata.content_sha256,
        records=metadata.record_count,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
