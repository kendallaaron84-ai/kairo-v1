"""Checkpointed Cloud Run orchestration for the frozen Stage 1 Theta pilot."""

from __future__ import annotations

import argparse
import multiprocessing
import os
import queue as queue_module
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import create_engine
from sqlalchemy.orm import Session


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
from engine.data.corpus_qualifier import (  # noqa: E402
    CorpusQualificationEngine,
)
from engine.data.option_enrollment import (  # noqa: E402
    CanonicalResolutionAccounting,
    HistoricalOptionEnrollmentGate,
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
from engine.data.streaming_pilot import (  # noqa: E402
    CanonicalJsonArrayWriter,
    OptionDiscoverySpool,
    RssBudgetTracker,
    SessionLiquidityIndex,
    StagedCorpusQualificationInput,
    StagedProviderUnit,
    ThetaDecodedArtifactExternalMerger,
    enforce_rss_budget,
    file_identity,
    iter_decoded_sections,
    normalize_staged_option_unit,
    persist_staged_pilot_atomically,
    qualify_staged,
    register_staged_dataset,
    release_unit_memory,
    staged_artifact,
)
from engine.validation.feed_loader import (  # noqa: E402
    DataNormalizer,
    HistoricalDatasetRegistry,
    StagedArtifact,
)
from engine.validation.models import StreamRole  # noqa: E402
from scripts.data.fetch_pilot_corpus import (  # noqa: E402
    _instrument,
    derive_strategy_001_decisions,
)


DEFAULT_BUCKET = "kairo-market-artifacts-507516"
DEFAULT_MANIFEST_OBJECT = "manifests/theta_q1_2024_manifest.json"
DEFAULT_STORAGE_ROOT = Path("/mnt/kairo-market-artifacts/historical-market")
ACQUISITION_POLICY_VERSION = "KAIRO-STAGE1-Q1-2024-v1"
AUTHORIZED_START = date(2024, 1, 2)
AUTHORIZED_END = date(2024, 3, 28)
TARGET_DTES = (0, 1, 7, 14, 30)
STRIKES_EACH_SIDE = 10
WORKER_RSS_LIMIT_MIB = 2560


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


def _apply_worker_memory_limit() -> None:
    if sys.platform == "win32":
        return
    import resource

    limit = WORKER_RSS_LIMIT_MIB * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def _fetch_unit_worker(
    unit: AcquisitionUnit,
    api_key: str,
    bucket_name: str,
    result_queue,
) -> None:
    """Own all SDK/native allocations for one missing provider unit."""
    try:
        _apply_worker_memory_limit()
        from thetadata import ThetaClient

        transport = ThetaDataV3ClientTransport(
            ThetaClient(api_key=api_key, dataframe_type="polars"),
            allow_paid_historical_retrieval=True,
        )
        adapter = ThetaDataProviderAdapter(transport)
        child_store = GCSCheckpointStore.from_default_credentials(bucket_name)
        if unit.endpoint == "stock_history_ohlc":
            provider_fetch = lambda: adapter.fetch_equity_minute_bars(
                symbol=unit.symbol,
                start_session=unit.session,
                end_session=unit.session,
            )
        else:
            assert unit.signal_at is not None
            provider_fetch = lambda: adapter.fetch_option_neighborhood(
                symbol=unit.symbol,
                signal_at=unit.signal_at,
                target_dtes=unit.target_dtes,
                strikes_each_side=unit.strikes_each_side,
            )
        metadata, fetched = acquire_or_resume(
            child_store,
            unit,
            provider_fetch,
            _record_count,
            sealed_at=datetime.now(timezone.utc),
        )
        result_queue.put((metadata.canonical_bytes(), fetched, None))
    except BaseException as error:
        result_queue.put((None, None, f"{type(error).__name__}: {error}"))
        raise


def ensure_checkpoint(
    store: GCSCheckpointStore,
    unit: AcquisitionUnit,
    *,
    api_key: str,
    bucket_name: str,
) -> CheckpointMetadata:
    """Reuse a sealed unit or isolate one provider fetch in a spawned process."""
    if store.checkpoint_exists(unit):
        metadata = store.load_metadata(unit)
        fetched = False
    else:
        context = multiprocessing.get_context("spawn")
        queue = context.Queue(maxsize=1)
        process = context.Process(
            target=_fetch_unit_worker,
            args=(unit, api_key, bucket_name, queue),
        )
        process.start()
        process.join()
        try:
            payload, fetched, error = queue.get(timeout=5)
        except queue_module.Empty:
            payload, fetched, error = None, None, None
        queue.close()
        if process.exitcode != 0 or error is not None or payload is None:
            raise RuntimeError(error or f"provider worker exited with code {process.exitcode}")
        metadata = store.load_metadata(unit)
    emit_structured_log(
        severity="INFO",
        event="ACQUISITION_UNIT_SEALED" if fetched else "ACQUISITION_UNIT_RESUMED",
        symbol=unit.symbol,
        session=unit.session,
        unit_key=unit.unit_key,
        content_sha256=metadata.content_sha256,
        records=metadata.record_count,
    )
    enforce_rss_budget(f"checkpoint:{unit.unit_key}")
    return metadata


def materialize_checkpoint(
    store: GCSCheckpointStore,
    unit: AcquisitionUnit,
    metadata: CheckpointMetadata,
    workspace: Path,
    mounted_bucket_root: Path | None = None,
) -> StagedProviderUnit:
    mounted_path = None if mounted_bucket_root is None else (
        mounted_bucket_root / store.artifact_path(metadata.content_sha256)
    )
    path = mounted_path if mounted_path is not None and mounted_path.exists() else (
        workspace / "provider-units" / metadata.content_sha256[:2] /
        (metadata.content_sha256 + ".bin")
    )
    if path.exists():
        digest, size = file_identity(path)
        if digest != metadata.content_sha256:
            raise ValueError("existing staged provider unit failed SHA-256 verification")
    else:
        digest, size = store.materialize_artifact(metadata, path)
    return StagedProviderUnit(
        unit_key=unit.unit_key,
        symbol=unit.symbol,
        session=unit.session,
        signal_at=unit.signal_at,
        artifact=StagedArtifact(
            path=path.resolve(),
            content_sha256=digest,
            byte_size=size,
            mime_type=ThetaDecodedArtifactSerializer.MIME_TYPE,
        ),
        record_count=metadata.record_count,
    )


def read_staged_provider(unit: StagedProviderUnit, request_kind: str) -> RawProviderArtifact:
    content = unit.artifact.path.read_bytes()
    artifact = RawProviderArtifact(
        provider_code="THETA_DATA",
        request_kind=request_kind,
        content=content,
        mime_type=unit.artifact.mime_type,
        request_parameters=(("unit_key", unit.unit_key),),
        artifact_type=ProviderArtifactType.DECODED_PROVIDER_ARTIFACT,
        format_version=ThetaDecodedArtifactSerializer.FORMAT_VERSION,
        serializer_version=ThetaDecodedArtifactSerializer.SERIALIZER_VERSION,
    )
    ThetaDecodedArtifactReader().read_provider_artifact(artifact)
    return artifact


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


def normalize_staged_underlyings(
    engine,
    calendar: SessionCalendarResolver,
    symbols: tuple[str, ...],
    units: dict[str, list[StagedProviderUnit]],
    raw_artifacts: dict[str, StagedArtifact],
    workspace: Path,
):
    """Normalize one underlying session at a time in one short read session."""
    instrument_ids = {}
    all_bars = []
    streams = []
    with Session(engine, expire_on_commit=False) as session:
        instruments = {symbol: _instrument(session, symbol) for symbol in symbols}
        normalizer = DataNormalizer(session, calendar)
        for ordinal, symbol in enumerate(symbols):
            normalized_path = workspace / "normalized" / f"{symbol}-bars.json"
            symbol_bars = []
            for unit in sorted(units[symbol], key=lambda item: item.session):
                artifact = read_staged_provider(
                    unit, ThetaDataProviderAdapter.BAR_REQUEST_KIND
                )
                decoded = ThetaDecodedArtifactReader().read_provider_artifact(artifact)
                bars = normalizer.normalize_theta_bars(
                    decoded.sections,
                    instrument_id=instruments[symbol].instrument_id,
                    symbol=symbol,
                )
                symbol_bars.extend(bars)
                del artifact, decoded, bars
                release_unit_memory()
                enforce_rss_budget(f"underlying-normalized:{symbol}:{unit.session}")
            symbol_bars.sort(key=lambda item: item.completed_at)
            if any(
                current.completed_at <= previous.completed_at
                for previous, current in zip(symbol_bars, symbol_bars[1:])
            ):
                raise ValueError("staged underlying bars are not strictly chronological")
            if not symbol_bars:
                raise ValueError(f"no canonical underlying bars were staged for {symbol}")
            with CanonicalJsonArrayWriter(normalized_path) as writer:
                for bar in symbol_bars:
                    writer.append(bar)
            all_bars.extend(symbol_bars)
            first = symbol_bars[0].interval_start_at
            last = symbol_bars[-1].completed_at
            count = len(symbol_bars)
            instrument_id = instruments[symbol].instrument_id
            instrument_ids[symbol] = instrument_id
            streams.append({
                "instrument_id": instrument_id,
                "symbol": symbol,
                "stream_role": StreamRole.UNDERLYING_SIGNAL_BARS,
                "stream_ordinal": ordinal,
                "raw_artifact": raw_artifacts[symbol],
                "normalized_artifact": staged_artifact(normalized_path, "application/json"),
                "bar_count": count,
                "first_bar_start_at": first,
                "last_bar_completed_at": last,
            })
    return instrument_ids, tuple(all_bars), tuple(streams)


def index_option_units(
    aggregate_artifacts: dict[str, StagedArtifact],
    workspace: Path,
) -> tuple[dict[str, OptionDiscoverySpool], dict[str, SessionLiquidityIndex]]:
    spools = {
        symbol: OptionDiscoverySpool(workspace / "indexes" / f"{symbol}-discoveries-v1.sqlite3", symbol)
        for symbol in aggregate_artifacts
    }
    liquidity = {
        symbol: SessionLiquidityIndex(workspace / "indexes" / f"{symbol}-liquidity-v1.sqlite3")
        for symbol in aggregate_artifacts
    }
    for symbol, artifact in aggregate_artifacts.items():
        for section_number, section in enumerate(iter_decoded_sections(artifact), start=1):
            spools[symbol].ingest_sections((section,), commit=False)
            liquidity[symbol].ingest_sections((section,), commit=False)
            del section
            if section_number % 100 == 0:
                release_unit_memory()
                enforce_rss_budget(f"option-indexed:{symbol}:{section_number}")
        spools[symbol].commit()
        liquidity[symbol].commit()
        release_unit_memory()
        enforce_rss_budget(f"option-indexed:{symbol}:complete")
    return spools, liquidity


def persist_staged_qualification(
    engine,
    calendar: SessionCalendarResolver,
    args: argparse.Namespace,
    symbols: tuple[str, ...],
    instrument_ids,
    all_bars,
    decisions,
    underlying_streams,
    option_units: dict[str, list[StagedProviderUnit]],
    option_raw_artifacts: dict[str, StagedArtifact],
    spools: dict[str, OptionDiscoverySpool],
    liquidity_indexes: dict[str, SessionLiquidityIndex],
    workspace: Path,
):
    """Enroll, normalize, register, and qualify under one final transaction."""
    def persist(session: Session):
        instruments = {symbol: _instrument(session, symbol) for symbol in symbols}
        if any(instruments[symbol].instrument_id != instrument_ids[symbol] for symbol in symbols):
            raise ValueError("canonical underlying identity changed between pipeline phases")
        normalizer = DataNormalizer(session, calendar)
        registry = HistoricalDatasetRegistry(session, args.storage_root)
        streams = list(underlying_streams)
        enrollment_accounting = []
        option_normalized_artifacts = []
        normalization_rss = RssBudgetTracker()
        normalized_units = 0
        for offset, symbol in enumerate(symbols, start=len(streams)):
            gate = HistoricalOptionEnrollmentGate(session)
            symbol_accounting = []
            for batch in spools[symbol].enrollment_batches():
                outcome = gate.enroll(
                    batch,
                    underlying_instrument_id=instruments[symbol].instrument_id,
                    underlying_symbol=symbol,
                    research_replay_mode=True,
                )
                spools[symbol].record_accepted(outcome.accepted_contract_keys)
                symbol_accounting.append(outcome.accounting)
            accounting = CanonicalResolutionAccounting.combine(symbol_accounting)
            enrollment_accounting.append(accounting)
            normalized_path = workspace / "normalized" / f"{symbol}-options.json"
            count = 0
            first = None
            last = None
            prior = None
            with CanonicalJsonArrayWriter(normalized_path) as writer:
                for unit in sorted(
                    option_units[symbol],
                    key=lambda item: item.signal_at or datetime.min.replace(tzinfo=timezone.utc),
                ):
                    snapshots = normalize_staged_option_unit(
                        unit,
                        normalizer=normalizer,
                        underlying_instrument_id=instruments[symbol].instrument_id,
                        symbol=symbol,
                        discovery_spool=spools[symbol],
                        liquidity_index=liquidity_indexes[symbol],
                    )
                    for snapshot in snapshots:
                        if prior is not None and snapshot.canonical_completed_at <= prior:
                            raise ValueError("staged option snapshots are not strictly chronological")
                        writer.append(snapshot)
                        prior = snapshot.canonical_completed_at
                        first = first or snapshot.canonical_completed_at
                        last = snapshot.canonical_completed_at
                        count += 1
                    del snapshots
                    release_unit_memory()
                    enforce_rss_budget(f"option-normalized:{symbol}:{unit.unit_key}")
                    normalized_units += 1
                    if normalized_units % 100 == 0:
                        normalization_rss.sample(
                            f"option-normalized-steady:{symbol}", normalized_units
                        )
            if count == 0 or first is None or last is None:
                raise ValueError(f"no canonical option snapshots were staged for {symbol}")
            normalized_artifact = staged_artifact(normalized_path, "application/json")
            option_normalized_artifacts.append(normalized_artifact)
            streams.append({
                "instrument_id": instruments[symbol].instrument_id,
                "symbol": symbol,
                "stream_role": StreamRole.OPTION_CHAIN_QUOTES,
                "stream_ordinal": offset,
                "raw_artifact": option_raw_artifacts[symbol],
                "normalized_artifact": normalized_artifact,
                "bar_count": count,
                "first_bar_start_at": first,
                "last_bar_completed_at": last,
            })
        normalization_rss.sample("option-normalized-final", normalized_units)
        normalization_rss.assert_bounded_growth()
        ingested_at = datetime.now(timezone.utc)
        dataset = register_staged_dataset(
            registry,
            dataset_name=f"THETA-PILOT-{args.start.isoformat()}-{args.end.isoformat()}",
            provider_name="THETA_DATA",
            bar_interval_seconds=60,
            source_timezone="America/New_York",
            source_timestamp_convention="INTERVAL_BEGIN",
            liquidity_fidelity_tier="TIER_1_QUOTE_DEPTH",
            price_adjustment_mode="RAW_UNADJUSTED",
            adjustment_policy_version=None,
            normalization_policy_version="NORM-PILOT-CORPUS-v1",
            ingested_at=ingested_at,
            streams=tuple(streams),
            calendar=calendar,
        )
        qualifier = CorpusQualificationEngine(session, calendar)
        manifest = qualify_staged(
            qualifier,
            StagedCorpusQualificationInput(
                provider_code="THETA_DATA",
                start_session=args.start,
                end_session=args.end,
                symbols=symbols,
                bar_artifacts=tuple(item["normalized_artifact"] for item in underlying_streams),
                option_snapshot_artifacts=tuple(option_normalized_artifacts),
                decision_points=decisions,
                raw_artifact_sha256s=tuple(
                    item["raw_artifact"].content_sha256 for item in streams
                ),
                normalized_dataset_manifest_sha256=dataset.dataset_manifest_sha256,
                resolution_accounting=CanonicalResolutionAccounting.combine(enrollment_accounting),
            ),
        )
        qualifier.persist_manifest(registry, manifest, created_at=ingested_at)
        return manifest

    return persist_staged_pilot_atomically(engine, persist)


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    environment = dict(os.environ)
    calendar = SessionCalendarResolver()
    symbols = validate_scope(args, environment, calendar)
    api_key = environment.get("THETADATA_API_KEY")
    database_url = environment.get("KAIRO_RUNTIME_DATABASE_URL")
    if not api_key or not database_url:
        raise RuntimeError("required Secret Manager environment injection is absent")

    store = GCSCheckpointStore.from_default_credentials(args.bucket)
    sessions = tuple(item[0] for item in calendar.sessions(args.start, args.end))
    workspace = args.storage_root / ".attempt-4-staging-v1"
    workspace.mkdir(parents=True, exist_ok=True)

    underlying_metadata: dict[str, list[tuple[AcquisitionUnit, CheckpointMetadata]]] = defaultdict(list)
    for symbol in symbols:
        for session in sessions:
            unit = underlying_unit(symbol, session)
            underlying_metadata[symbol].append(
                (
                    unit,
                    ensure_checkpoint(
                        store, unit, api_key=api_key, bucket_name=args.bucket
                    ),
                )
            )
    underlying_units = {
        symbol: [
            materialize_checkpoint(
                store, unit, metadata, workspace,
                mounted_bucket_root=args.storage_root.parent,
            )
            for unit, metadata in underlying_metadata[symbol]
        ]
        for symbol in symbols
    }
    merger = ThetaDecodedArtifactExternalMerger(workspace / "merge-work")
    underlying_raw = {
        symbol: merger.merge(
            [item.artifact for item in underlying_units[symbol]],
            request_kind=ThetaDataProviderAdapter.BAR_REQUEST_KIND,
            symbol=symbol,
            output_path=workspace / "aggregates" / f"{symbol}-bars.bin",
        ) for symbol in symbols
    }

    engine = create_engine(database_url)
    try:
        instrument_ids, all_bars, underlying_streams = normalize_staged_underlyings(
            engine, calendar, symbols, underlying_units, underlying_raw, workspace
        )
        decisions = derive_strategy_001_decisions(all_bars)

        option_metadata: dict[str, list[tuple[AcquisitionUnit, CheckpointMetadata]]] = defaultdict(list)
        acquisition_rss = RssBudgetTracker()
        acquired_option_units = 0
        for symbol in symbols:
            symbol_decisions = tuple(item for item in decisions if item.symbol == symbol)
            if not symbol_decisions:
                raise ValueError(f"Strategy 001 produced no pilot decision points for {symbol}")
            for decision in symbol_decisions:
                unit = option_unit(symbol, decision.signal_at)
                option_metadata[symbol].append(
                    (
                        unit,
                        ensure_checkpoint(
                            store, unit, api_key=api_key, bucket_name=args.bucket
                        ),
                    )
                )
                acquired_option_units += 1
                if acquired_option_units % 100 == 0:
                    acquisition_rss.sample(
                        f"option-acquisition-steady:{symbol}", acquired_option_units
                    )
        acquisition_rss.sample("option-acquisition-final", acquired_option_units)
        acquisition_rss.assert_bounded_growth()
        option_units = {
            symbol: [
                materialize_checkpoint(
                    store, unit, metadata, workspace,
                    mounted_bucket_root=args.storage_root.parent,
                )
                for unit, metadata in option_metadata[symbol]
            ]
            for symbol in symbols
        }
        option_raw = {
            symbol: merger.merge(
                [item.artifact for item in option_units[symbol]],
                request_kind=ThetaDataProviderAdapter.OPTION_REQUEST_KIND,
                symbol=symbol,
                output_path=workspace / "aggregates" / f"{symbol}-options.bin",
            ) for symbol in symbols
        }
        local_index_workspace = Path(tempfile.mkdtemp(prefix="kairo-pilot-indexes-"))
        spools, liquidity_indexes = index_option_units(option_raw, local_index_workspace)

        manifest = persist_then_seal_manifest(
            lambda: persist_staged_qualification(
                engine,
                calendar,
                args,
                symbols,
                instrument_ids,
                all_bars,
                decisions,
                underlying_streams,
                option_units,
                option_raw,
                spools,
                liquidity_indexes,
                workspace,
            ),
            store,
            args.manifest_object,
        )
        print(manifest.canonical_bytes().decode("utf-8"), flush=True)
    finally:
        for resource in (*locals().get("spools", {}).values(), *locals().get("liquidity_indexes", {}).values()):
            resource.__exit__(None, None, None)
        local_indexes = locals().get("local_index_workspace")
        if local_indexes is not None:
            shutil.rmtree(local_indexes)
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
