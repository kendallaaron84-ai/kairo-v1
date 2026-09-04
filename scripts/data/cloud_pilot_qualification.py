"""Checkpointed Cloud Run orchestration for the frozen Stage 1 Theta pilot."""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import create_engine


ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = ROOT / "backend"
for import_root in (ROOT, BACKEND_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from app.infrastructure.storage.gcs_checkpoint import (  # noqa: E402
    AcquisitionUnit,
    CheckpointMetadata,
    GCSCheckpointStore,
    acquire_or_resume,
    emit_structured_log,
)
from engine.data.provider_adapter import (  # noqa: E402
    ProviderArtifactType,
    RawProviderArtifact,
    ThetaDataProviderAdapter,
)
from engine.data.theta_v3 import (  # noqa: E402
    DecodedThetaSection,
    ThetaDataV3ClientTransport,
    ThetaDecodedArtifactReader,
    ThetaDecodedArtifactSerializer,
)
from engine.validation.session_calendar import SessionCalendarResolver  # noqa: E402
from scripts.data.fetch_pilot_corpus import (  # noqa: E402
    derive_strategy_001_decisions,
    normalize_underlying_evidence,
    persist_pilot_atomically,
)


DEFAULT_BUCKET = "kairo-market-artifacts-507516"
DEFAULT_MANIFEST_OBJECT = "manifests/theta_q1_2024_manifest.json"
DEFAULT_STORAGE_ROOT = Path("/mnt/kairo-market-artifacts/historical-market")
ACQUISITION_POLICY_VERSION = "KAIRO-STAGE1-Q1-2024-v1"
AUTHORIZED_START = date(2024, 1, 2)
AUTHORIZED_END = date(2024, 3, 28)
TARGET_DTES = (0, 1, 7, 14, 30)
STRIKES_EACH_SIDE = 10


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("theta",), required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--qualify", action="store_true", required=True)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--manifest-object", default=DEFAULT_MANIFEST_OBJECT)
    parser.add_argument("--storage-root", type=Path, default=DEFAULT_STORAGE_ROOT)
    parser.add_argument("--authorize-paid-theta-history", action="store_true")
    return parser.parse_args(argv)


def validate_scope(
    args: argparse.Namespace,
    environment: dict[str, str],
    calendar: SessionCalendarResolver,
) -> tuple[str, ...]:
    symbols = tuple(value.strip().upper() for value in args.symbols.split(",") if value.strip())
    if symbols != ("TQQQ", "SQQQ"):
        raise ValueError("the frozen Stage 1 Theta pilot requires symbols TQQQ,SQQQ")
    if args.start != AUTHORIZED_START or args.end != AUTHORIZED_END:
        raise ValueError("Attempt #4 requires exactly 2024-01-02 through 2024-03-28")
    session_count = len(calendar.sessions(args.start, args.end))
    if session_count < 30 or session_count > 61:
        raise ValueError("Stage 1 pilot window must contain 30 through 61 canonical sessions")
    if not args.authorize_paid_theta_history:
        raise PermissionError("--authorize-paid-theta-history is required")
    if environment.get("KAIRO_CLOUD_PILOT_AUTHORIZED") != "1":
        raise PermissionError("KAIRO_CLOUD_PILOT_AUTHORIZED=1 is required")
    if args.storage_root != DEFAULT_STORAGE_ROOT:
        raise ValueError(
            "pilot storage root must be /mnt/kairo-market-artifacts/historical-market"
        )
    if args.manifest_object != DEFAULT_MANIFEST_OBJECT:
        raise ValueError("pilot manifest object must use the canonical Q1 2024 path")
    return symbols


def underlying_unit(symbol: str, session: date) -> AcquisitionUnit:
    return AcquisitionUnit(
        provider="THETA_DATA",
        endpoint="stock_history_ohlc",
        symbol=symbol,
        session=session,
        signal_at=None,
        target_dtes=(),
        strikes_each_side=0,
        serializer_version=ThetaDecodedArtifactSerializer.SERIALIZER_VERSION,
        acquisition_policy_version=ACQUISITION_POLICY_VERSION,
    )


def option_unit(symbol: str, signal_at: datetime) -> AcquisitionUnit:
    if signal_at.tzinfo is None:
        raise ValueError("signal timestamp must be timezone-aware")
    eastern = SessionCalendarResolver.eastern
    return AcquisitionUnit(
        provider="THETA_DATA",
        endpoint=ThetaDataProviderAdapter.OPTION_REQUEST_KIND,
        symbol=symbol,
        session=signal_at.astimezone(eastern).date(),
        signal_at=signal_at.astimezone(timezone.utc),
        target_dtes=TARGET_DTES,
        strikes_each_side=STRIKES_EACH_SIDE,
        serializer_version=ThetaDecodedArtifactSerializer.SERIALIZER_VERSION,
        acquisition_policy_version=ACQUISITION_POLICY_VERSION,
    )


def _record_count(artifact: RawProviderArtifact) -> int:
    decoded = ThetaDecodedArtifactReader().read_provider_artifact(artifact)
    return sum(len(section.records) for section in decoded.sections)


def acquire_unit(
    store: GCSCheckpointStore,
    unit: AcquisitionUnit,
    provider_fetch,
) -> RawProviderArtifact:
    metadata, fetched = acquire_or_resume(
        store,
        unit,
        provider_fetch,
        _record_count,
        sealed_at=datetime.now(timezone.utc),
    )
    content = store.read_artifact(metadata)
    artifact = _materialized_artifact(unit, metadata, content)
    ThetaDecodedArtifactReader().read_provider_artifact(artifact)
    emit_structured_log(
        severity="INFO",
        event="ACQUISITION_UNIT_SEALED" if fetched else "ACQUISITION_UNIT_RESUMED",
        symbol=unit.symbol,
        session=unit.session,
        unit_key=unit.unit_key,
        content_sha256=metadata.content_sha256,
        records=metadata.record_count,
    )
    return artifact


def _materialized_artifact(
    unit: AcquisitionUnit,
    metadata: CheckpointMetadata,
    content: bytes,
) -> RawProviderArtifact:
    request_kind = (
        ThetaDataProviderAdapter.BAR_REQUEST_KIND
        if unit.endpoint == "stock_history_ohlc"
        else ThetaDataProviderAdapter.OPTION_REQUEST_KIND
    )
    return RawProviderArtifact(
        provider_code="THETA_DATA",
        request_kind=request_kind,
        content=content,
        mime_type=ThetaDecodedArtifactSerializer.MIME_TYPE,
        request_parameters=(("unit_key", unit.unit_key),),
        artifact_type=ProviderArtifactType.DECODED_PROVIDER_ARTIFACT,
        format_version=metadata.format_version,
        serializer_version=metadata.serializer_version,
    )


def aggregate_artifacts(
    artifacts: Iterable[RawProviderArtifact],
    *,
    request_kind: str,
    symbol: str,
) -> RawProviderArtifact:
    reader = ThetaDecodedArtifactReader()
    decoded_artifacts = sorted(
        (reader.read_provider_artifact(artifact) for artifact in artifacts),
        key=lambda value: value.content_sha256,
    )
    if not decoded_artifacts:
        raise ValueError(f"no provider artifacts were acquired for {symbol}")
    sections = []
    wire_hashes = set()
    component_hashes = []
    for decoded in decoded_artifacts:
        component_hashes.append(decoded.content_sha256)
        wire_hashes.update(decoded.source_wire_sha256s)
        sections.extend(
            DecodedThetaSection(
                endpoint=section.endpoint,
                parameters=section.parameters,
                dataframe=section.missing_evidence or section.records,
            )
            for section in decoded.sections
        )
    acquisition_request = {
        "request_kind": request_kind,
        "symbol": symbol,
        "component_content_sha256s": ",".join(component_hashes),
    }
    serializer = ThetaDecodedArtifactSerializer()
    content = serializer.serialize(
        sections,
        acquisition_request=acquisition_request,
        source_wire_sha256s=tuple(sorted(wire_hashes)),
    )
    return RawProviderArtifact(
        provider_code="THETA_DATA",
        request_kind=request_kind,
        content=content,
        mime_type=serializer.MIME_TYPE,
        request_parameters=tuple(
            sorted((key, str(value)) for key, value in acquisition_request.items())
        ),
        artifact_type=ProviderArtifactType.DECODED_PROVIDER_ARTIFACT,
        format_version=serializer.FORMAT_VERSION,
        serializer_version=serializer.SERIALIZER_VERSION,
    )


def seal_manifest_after_commit(
    store: GCSCheckpointStore,
    object_name: str,
    manifest,
) -> bool:
    """Seal only a manifest returned after atomic persistence committed."""
    return store.seal_manifest(object_name, manifest.canonical_bytes())


def persist_then_seal_manifest(
    persistence_call,
    store: GCSCheckpointStore,
    object_name: str,
):
    """Make successful database persistence a hard prerequisite for GCS sealing."""
    manifest = persistence_call()
    seal_manifest_after_commit(store, object_name, manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    environment = dict(os.environ)
    calendar = SessionCalendarResolver()
    symbols = validate_scope(args, environment, calendar)
    api_key = environment.get("THETADATA_API_KEY")
    database_url = environment.get("KAIRO_RUNTIME_DATABASE_URL")
    if not api_key or not database_url:
        raise RuntimeError("required Secret Manager environment injection is absent")

    from thetadata import ThetaClient

    transport = ThetaDataV3ClientTransport(
        ThetaClient(api_key=api_key, dataframe_type="polars"),
        allow_paid_historical_retrieval=True,
    )
    adapter = ThetaDataProviderAdapter(transport)
    store = GCSCheckpointStore.from_default_credentials(args.bucket)
    sessions = tuple(item[0] for item in calendar.sessions(args.start, args.end))

    underlying_components: dict[str, list[RawProviderArtifact]] = defaultdict(list)
    for symbol in symbols:
        for session in sessions:
            unit = underlying_unit(symbol, session)
            underlying_components[symbol].append(
                acquire_unit(
                    store,
                    unit,
                    lambda symbol=symbol, session=session: adapter.fetch_equity_minute_bars(
                        symbol=symbol,
                        start_session=session,
                        end_session=session,
                    ),
                )
            )
    underlying_responses = {
        symbol: aggregate_artifacts(
            underlying_components[symbol],
            request_kind=ThetaDataProviderAdapter.BAR_REQUEST_KIND,
            symbol=symbol,
        )
        for symbol in symbols
    }

    engine = create_engine(database_url)
    try:
        instrument_ids, all_bars, underlying_streams = normalize_underlying_evidence(
            engine, calendar, symbols, underlying_responses
        )
        decisions = derive_strategy_001_decisions(all_bars)

        option_components: dict[str, list[RawProviderArtifact]] = defaultdict(list)
        for symbol in symbols:
            symbol_decisions = tuple(item for item in decisions if item.symbol == symbol)
            if not symbol_decisions:
                raise ValueError(f"Strategy 001 produced no pilot decision points for {symbol}")
            for decision in symbol_decisions:
                unit = option_unit(symbol, decision.signal_at)
                option_components[symbol].append(
                    acquire_unit(
                        store,
                        unit,
                        lambda symbol=symbol, signal_at=decision.signal_at: (
                            adapter.fetch_option_neighborhood(
                                symbol=symbol,
                                signal_at=signal_at,
                                target_dtes=TARGET_DTES,
                                strikes_each_side=STRIKES_EACH_SIDE,
                            )
                        ),
                    )
                )
        option_responses = {
            symbol: aggregate_artifacts(
                option_components[symbol],
                request_kind=ThetaDataProviderAdapter.OPTION_REQUEST_KIND,
                symbol=symbol,
            )
            for symbol in symbols
        }

        manifest = persist_then_seal_manifest(
            lambda: persist_pilot_atomically(
                engine,
                calendar,
                args,
                symbols,
                instrument_ids,
                all_bars,
                decisions,
                underlying_responses,
                underlying_streams,
                option_responses,
            ),
            store,
            args.manifest_object,
        )
        print(manifest.canonical_bytes().decode("utf-8"), flush=True)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
