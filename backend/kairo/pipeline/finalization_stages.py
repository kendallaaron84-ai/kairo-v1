"""Durable stages for the immutable Q1 2024 finalization corpus."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.models.configuration import Instrument
from app.db.models.historical import (
    HistoricalMarketArtifact,
    HistoricalMarketDataset,
    HistoricalMarketDatasetSymbol,
)
from app.infrastructure.storage.gcs_checkpoint import (
    AcquisitionUnit,
    CheckpointMetadata,
    GCSCheckpointStore,
)
from engine.data.corpus_qualifier import CorpusQualificationEngine, PilotDecisionPoint
from engine.data.option_enrollment import (
    CanonicalResolutionAccounting,
    HistoricalOptionEnrollmentGate,
    canonical_occ_symbol,
    deterministic_option_instrument_id,
)

from engine.data.streaming_pilot import (
    OptionDiscoverySpool,
    SessionLiquidityIndex,
    current_rss_mib,
    CanonicalJsonArrayWriter,
    StagedCorpusQualificationInput,
    StagedProviderUnit,
    iter_canonical_json_array,
    normalize_staged_option_unit,
    qualify_staged,
    register_staged_dataset,
    release_unit_memory,
    scan_decoded_aggregate,
    staged_artifact,
    file_identity,
)
from engine.data.provider_adapter import ThetaDataProviderAdapter
from engine.data.theta_v3 import ThetaDecodedArtifactSerializer
from engine.validation.feed_loader import DataNormalizer, HistoricalDatasetRegistry, StagedArtifact
from engine.validation.models import CanonicalMarketBar, CanonicalOptionChainSnapshot, StreamRole
from engine.validation.session_calendar import SessionCalendarResolver
from engine.strategy.ema_cross_strategy import EMACrossStrategy
from kairo.pipeline.finalization_state import (
    AUTHORITY_STAGE_VERSION,
    DATASET,
    INDEX_SCHEMA_VERSION,
    MANIFEST_STAGE_VERSION,
    NORMALIZATION_STAGE_VERSION,
    PREFIX,
    RECEIPT_VERSION,
    DurableObjectStore,
    ObjectIdentity,
    Receipt,
    load_receipt,
    restore_verified_object,
    seal_receipt,
    sha256_file,
)
SYMBOLS = ("TQQQ", "SQQQ")
AUTHORIZED_START = date(2024, 1, 2)
AUTHORIZED_END = date(2024, 3, 28)
TARGET_DTES = (0, 1, 7, 14, 30)
STRIKES_EACH_SIDE = 10
ACQUISITION_POLICY_VERSION = "KAIRO-STAGE1-Q1-2024-v1"
SOURCE_OBJECTS = {
    symbol: f"historical-market/.attempt-4-staging-v1/aggregates/{symbol}-options.bin"
    for symbol in SYMBOLS
}
SOURCE_MOUNT_PATHS = {
    symbol: Path("/mnt/kairo-market-artifacts") / SOURCE_OBJECTS[symbol]
    for symbol in SYMBOLS
}
UNDERLYING_SOURCE_OBJECTS = {
    symbol: f"historical-market/.attempt-4-staging-v1/aggregates/{symbol}-bars.bin"
    for symbol in SYMBOLS
}
MOUNT_ROOT = Path("/mnt/kairo-market-artifacts")
STORAGE_ROOT = MOUNT_ROOT / "historical-market"


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
    return AcquisitionUnit(
        provider="THETA_DATA",
        endpoint=ThetaDataProviderAdapter.OPTION_REQUEST_KIND,
        symbol=symbol,
        session=signal_at.astimezone(SessionCalendarResolver.eastern).date(),
        signal_at=signal_at.astimezone(timezone.utc),
        target_dtes=TARGET_DTES,
        strikes_each_side=STRIKES_EACH_SIDE,
        serializer_version=ThetaDecodedArtifactSerializer.SERIALIZER_VERSION,
        acquisition_policy_version=ACQUISITION_POLICY_VERSION,
    )


def materialize_checkpoint(
    store: GCSCheckpointStore,
    unit: AcquisitionUnit,
    metadata: CheckpointMetadata,
    workspace: Path,
    mounted_bucket_root: Path,
) -> StagedProviderUnit:
    mounted_path = mounted_bucket_root / store.artifact_path(metadata.content_sha256)
    path = mounted_path if mounted_path.exists() else (
        workspace / "provider-units" / metadata.content_sha256[:2]
        / f"{metadata.content_sha256}.bin"
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


def _instrument(session: Session, symbol: str) -> Instrument:
    row = session.scalar(select(Instrument).where(
        Instrument.symbol == symbol,
        Instrument.asset_class != "OPTION",
        Instrument.retired_at.is_(None),
    ))
    if row is None:
        raise ValueError(f"canonical underlying instrument is absent: {symbol}")
    return row


def derive_strategy_001_decisions(
    bars: tuple[CanonicalMarketBar, ...],
) -> tuple[PilotDecisionPoint, ...]:
    by_session: dict[date, list[CanonicalMarketBar]] = defaultdict(list)
    for bar in bars:
        session_date = bar.completed_at.astimezone(SessionCalendarResolver.eastern).date()
        by_session[session_date].append(bar)
    decisions = []
    for session_date in sorted(by_session):
        strategy = EMACrossStrategy(settled_cash=Decimal("0"))
        for bar in sorted(
            by_session[session_date], key=lambda item: (item.completed_at, item.symbol)
        ):
            strategy.on_bar(symbol=bar.symbol, close=bar.close, timestamp=bar.completed_at)
            if strategy.last_missing_execution_evidence == "ENTRY_OPTION_QUOTE_MISSING":
                decisions.append(PilotDecisionPoint(
                    underlying_instrument_id=bar.instrument_id,
                    symbol=bar.symbol,
                    signal_at=bar.completed_at,
                    underlying_spot=bar.close,
                ))
    return tuple(decisions)


def _identity(identity: ObjectIdentity) -> dict[str, Any]:
    return asdict(identity)


def _output(object_name: str, identity: ObjectIdentity, **facts: Any) -> dict[str, Any]:
    return {"object_name": object_name, "identity": _identity(identity), **facts}


class Progress:
    def __init__(self, emit: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.started = time.perf_counter()
        self.emit = emit or self._print

    @staticmethod
    def _print(value: dict[str, Any]) -> None:
        print(json.dumps(value, sort_keys=True, separators=(",", ":")), flush=True)

    def event(self, event: str, stage: int, stage_started: float, **facts: Any) -> None:
        now = time.perf_counter()
        self.emit({
            "severity": "INFO",
            "event": event,
            "stage": stage,
            "elapsed_seconds": round(now - stage_started, 3),
            "cumulative_seconds": round(now - self.started, 3),
            "rss_mib": round(current_rss_mib(), 3),
            **facts,
        })


def validate_object_descriptor(
    store: DurableObjectStore, descriptor: dict[str, Any], *, require_sha_metadata: bool
) -> ObjectIdentity:
    actual = store.stat(descriptor["object_name"])
    if actual is None:
        raise ValueError(f"receipt input/output is absent: {descriptor['object_name']}")
    expected = ObjectIdentity(**descriptor["identity"])
    if (
        actual.uri != expected.uri
        or actual.generation != expected.generation
        or actual.metageneration != expected.metageneration
        or actual.byte_count != expected.byte_count
        or (require_sha_metadata and actual.sha256 != expected.sha256)
    ):
        raise ValueError(f"receipt object identity contradiction: {descriptor['object_name']}")
    return actual


def validate_receipt(
    store: DurableObjectStore,
    receipt: Receipt,
    *,
    expected_stage: int,
    predecessor: Receipt | None,
) -> None:
    if receipt.stage != expected_stage:
        raise ValueError("finalization receipt stage mismatch")
    expected_version = {
        1: INDEX_SCHEMA_VERSION,
        2: NORMALIZATION_STAGE_VERSION,
        3: AUTHORITY_STAGE_VERSION,
        4: MANIFEST_STAGE_VERSION,
    }[expected_stage]
    if receipt.stage_version != expected_version:
        raise ValueError("finalization receipt stage version mismatch")
    expected_predecessor = None if predecessor is None else predecessor.sha256
    if receipt.predecessor_sha256 != expected_predecessor:
        raise ValueError("finalization receipt predecessor contradiction")
    for descriptor in receipt.inputs:
        validate_object_descriptor(store, descriptor, require_sha_metadata=False)
    for descriptor in receipt.outputs:
        validate_object_descriptor(store, descriptor, require_sha_metadata=True)


def run_stage_1(
    store: DurableObjectStore,
    workspace: Path,
    progress: Progress,
    *,
    source_paths: dict[str, Path] | None = None,
) -> Receipt:
    """Verify aggregate bytes and durably commit both indexes per symbol."""
    started = time.perf_counter()
    progress.event("FINALIZATION_STAGE_STARTED", 1, started)
    sources = source_paths or SOURCE_MOUNT_PATHS
    input_descriptors: list[dict[str, Any]] = []
    output_descriptors: list[dict[str, Any]] = []
    aggregate_facts: dict[str, Any] = {}
    pending_indexes: list[tuple[str, str, Path, dict[str, int]]] = []

    for symbol in SYMBOLS:
        symbol_started = time.perf_counter()
        source_name = SOURCE_OBJECTS[symbol]
        before = store.stat(source_name)
        if before is None:
            raise FileNotFoundError(f"immutable aggregate is absent: {source_name}")
        source_path = sources[symbol]
        if not source_path.is_file():
            raise FileNotFoundError(f"mounted aggregate is absent: {source_path}")
        symbol_dir = workspace / "stage-1" / symbol
        if symbol_dir.exists():
            shutil.rmtree(symbol_dir)
        symbol_dir.mkdir(parents=True)
        discoveries_path = symbol_dir / "discoveries.sqlite3"
        liquidity_path = symbol_dir / "liquidity.sqlite3"
        last_report = 0

        with OptionDiscoverySpool(discoveries_path, symbol) as discoveries, SessionLiquidityIndex(
            liquidity_path
        ) as liquidity:
            discoveries.connection.execute("BEGIN IMMEDIATE")
            liquidity.connection.execute("BEGIN IMMEDIATE")

            def sink(section: Any) -> None:
                discoveries.ingest_sections((section,), commit=False)
                liquidity.ingest_sections((section,), commit=False)

            def report(bytes_processed: int, frame_count: int) -> None:
                nonlocal last_report
                if bytes_processed - last_report < 1024 * 1024 * 1024:
                    return
                last_report = bytes_processed
                elapsed = max(time.perf_counter() - symbol_started, 0.001)
                progress.event(
                    "FINALIZATION_STAGE_PROGRESS",
                    1,
                    started,
                    symbol=symbol,
                    bytes_processed=bytes_processed,
                    total_bytes=before.byte_count,
                    throughput_mib_s=round(bytes_processed / (1024 * 1024) / elapsed, 3),
                    frame_counts=frame_count,
                    sqlite_rows={
                        **discoveries.row_counts(),
                        "oi": liquidity.row_count(),
                    },
                    sqlite_bytes=sum(
                        path.stat().st_size for path in (discoveries_path, liquidity_path)
                        if path.exists()
                    ),
                    source_generation=before.generation,
                )

            try:
                verified = scan_decoded_aggregate(
                    source_path,
                    expected_byte_size=before.byte_count,
                    section_sinks=(sink,),
                    progress=report,
                )
                after = store.stat(source_name)
                if after is None or (
                    before.generation,
                    before.metageneration,
                    before.byte_count,
                ) != (after.generation, after.metageneration, after.byte_count):
                    raise ValueError("immutable aggregate changed during verification")
                discoveries.commit()
                liquidity.commit()
                discovery_rows = discoveries.row_counts()
                liquidity_rows = liquidity.row_count()
            except BaseException:
                discoveries.rollback()
                liquidity.rollback()
                raise

        source_identity = ObjectIdentity(
            uri=before.uri,
            generation=before.generation,
            metageneration=before.metageneration,
            byte_count=verified.artifact.byte_size,
            sha256=verified.artifact.content_sha256,
        )
        input_descriptors.append({
            "object_name": source_name,
            "identity": _identity(source_identity),
            "symbol": symbol,
        })
        for kind, path, rows in (
            ("discoveries", discoveries_path, discovery_rows),
            ("liquidity", liquidity_path, {"oi": liquidity_rows}),
        ):
            pending_indexes.append((symbol, kind, path, rows))
        aggregate_facts[symbol] = {
            "source_sha256": verified.artifact.content_sha256,
            "source_byte_count": verified.artifact.byte_size,
            "section_count": verified.section_count,
            "frame_count": verified.frame_count,
        }
        symbol_elapsed = max(time.perf_counter() - symbol_started, 0.001)
        progress.event(
            "FINALIZATION_AGGREGATE_VERIFIED",
            1,
            started,
            symbol=symbol,
            bytes_processed=verified.artifact.byte_size,
            total_bytes=before.byte_count,
            throughput_mib_s=round(
                verified.artifact.byte_size / (1024 * 1024) / symbol_elapsed, 3
            ),
            frame_counts=verified.frame_count,
            sqlite_rows={**discovery_rows, "oi": liquidity_rows},
            sqlite_bytes=discoveries_path.stat().st_size + liquidity_path.stat().st_size,
            source_generation=before.generation,
            source_hash=verified.artifact.content_sha256,
        )

    # No object is published until every source aggregate has passed verification.
    for symbol, kind, path, rows in pending_indexes:
        digest, size = sha256_file(path)
        object_name = f"{PREFIX}/indexes/{symbol}-{kind}-{digest}.sqlite3"
        uploaded = store.upload_immutable(
            object_name,
            path,
            content_type="application/vnd.sqlite3",
            sha256=digest,
            byte_count=size,
        )
        output_descriptors.append(_output(
            object_name,
            uploaded,
            symbol=symbol,
            index_kind=kind,
            schema_version=INDEX_SCHEMA_VERSION,
            sqlite_rows=rows,
        ))

    receipt = Receipt(
        receipt_version=RECEIPT_VERSION,
        dataset=DATASET,
        stage=1,
        stage_version=INDEX_SCHEMA_VERSION,
        predecessor_sha256=None,
        inputs=tuple(input_descriptors),
        outputs=tuple(output_descriptors),
        facts={"aggregates": aggregate_facts},
    )
    seal_receipt(store, receipt)
    progress.event("FINALIZATION_STAGE_COMPLETED", 1, started, receipt_sha256=receipt.sha256)
    return receipt


def restore_stage_1_indexes(
    store: DurableObjectStore, receipt: Receipt, workspace: Path
) -> tuple[dict[str, OptionDiscoverySpool], dict[str, SessionLiquidityIndex]]:
    discoveries: dict[str, OptionDiscoverySpool] = {}
    liquidity: dict[str, SessionLiquidityIndex] = {}
    for descriptor in receipt.outputs:
        validate_object_descriptor(store, descriptor, require_sha_metadata=True)
        target = workspace / "restored-indexes" / f"{descriptor['symbol']}-{descriptor['index_kind']}.sqlite3"
        restore_verified_object(store, descriptor, target)
        if descriptor["index_kind"] == "discoveries":
            discoveries[descriptor["symbol"]] = OptionDiscoverySpool(target, descriptor["symbol"])
        elif descriptor["index_kind"] == "liquidity":
            liquidity[descriptor["symbol"]] = SessionLiquidityIndex(target)
        else:
            raise ValueError("unknown Stage 1 index kind")
    if set(discoveries) != set(SYMBOLS) or set(liquidity) != set(SYMBOLS):
        raise ValueError("Stage 1 receipt does not cover both symbols and indexes")
    return discoveries, liquidity


def _write_bytes(path: Path, content: bytes) -> tuple[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(content).hexdigest(), len(content)


def _upload_file(
    store: DurableObjectStore,
    object_name: str,
    path: Path,
    content_type: str,
    **facts: Any,
) -> dict[str, Any]:
    digest, size = sha256_file(path)
    identity = store.upload_immutable(
        object_name, path, content_type=content_type, sha256=digest, byte_count=size
    )
    return _output(object_name, identity, **facts)


def _load_only_units(
    checkpoint_store: GCSCheckpointStore,
    workspace: Path,
    calendar: SessionCalendarResolver,
    database_url: str,
) -> tuple[
    dict[str, Any],
    tuple[CanonicalMarketBar, ...],
    tuple[PilotDecisionPoint, ...],
    tuple[dict[str, Any], ...],
    dict[str, list[StagedProviderUnit]],
    tuple[dict[str, Any], ...],
]:
    """Reconstruct the frozen plan exclusively from already sealed checkpoints."""
    sessions = tuple(item[0] for item in calendar.sessions(AUTHORIZED_START, AUTHORIZED_END))
    underlying_units: dict[str, list[StagedProviderUnit]] = {}
    for symbol in SYMBOLS:
        values = []
        for session_date in sessions:
            unit = underlying_unit(symbol, session_date)
            metadata = checkpoint_store.load_metadata(unit)
            values.append(materialize_checkpoint(
                checkpoint_store,
                unit,
                metadata,
                workspace,
                mounted_bucket_root=MOUNT_ROOT,
            ))
        underlying_units[symbol] = values

    engine = create_engine(database_url)
    try:
        instrument_ids: dict[str, Any] = {}
        all_bars: list[CanonicalMarketBar] = []
        streams: list[dict[str, Any]] = []
        raw_descriptors: list[dict[str, Any]] = []
        with Session(engine) as session:
            instruments = {symbol: _instrument(session, symbol) for symbol in SYMBOLS}
            normalizer = DataNormalizer(session, calendar)
            for ordinal, symbol in enumerate(SYMBOLS):
                source_name = UNDERLYING_SOURCE_OBJECTS[symbol]
                source_path = MOUNT_ROOT / source_name
                source_stat = checkpoint_store._bucket.blob(source_name)
                if not source_stat.exists() or not source_path.is_file():
                    raise FileNotFoundError(f"sealed underlying aggregate is absent: {source_name}")
                source_stat.reload()
                before_identity = (
                    str(source_stat.generation),
                    str(source_stat.metageneration),
                    int(source_stat.size),
                )
                verified = scan_decoded_aggregate(
                    source_path, expected_byte_size=before_identity[2]
                )
                source_stat.reload()
                if before_identity != (
                    str(source_stat.generation),
                    str(source_stat.metageneration),
                    int(source_stat.size),
                ):
                    raise ValueError("underlying aggregate changed during verification")
                source_identity = ObjectIdentity(
                    uri=f"gs://{checkpoint_store._bucket.name}/{source_name}",
                    generation=str(source_stat.generation),
                    metageneration=str(source_stat.metageneration),
                    byte_count=verified.artifact.byte_size,
                    sha256=verified.artifact.content_sha256,
                )
                raw_descriptors.append({
                    "object_name": source_name,
                    "identity": _identity(source_identity),
                    "symbol": symbol,
                    "stream_role": str(StreamRole.UNDERLYING_SIGNAL_BARS),
                })
                bars: list[CanonicalMarketBar] = []
                from engine.data.streaming_pilot import iter_decoded_sections

                for unit_value in underlying_units[symbol]:
                    bars.extend(normalizer.normalize_theta_bars(
                        iter_decoded_sections(unit_value.artifact),
                        instrument_id=instruments[symbol].instrument_id,
                        symbol=symbol,
                    ))
                bars.sort(key=lambda item: item.completed_at)
                if not bars or any(
                    current.completed_at <= previous.completed_at
                    for previous, current in zip(bars, bars[1:])
                ):
                    raise ValueError("underlying normalization ordering invariant failed")
                normalized_path = workspace / "stage-2" / f"{symbol}-bars.json"
                with CanonicalJsonArrayWriter(normalized_path) as writer:
                    for bar in bars:
                        writer.append(bar)
                instrument_ids[symbol] = instruments[symbol].instrument_id
                all_bars.extend(bars)
                streams.append({
                    "instrument_id": str(instruments[symbol].instrument_id),
                    "symbol": symbol,
                    "stream_role": str(StreamRole.UNDERLYING_SIGNAL_BARS),
                    "stream_ordinal": ordinal,
                    "normalized_path": str(normalized_path),
                    "bar_count": len(bars),
                    "first_bar_start_at": bars[0].interval_start_at.isoformat(),
                    "last_bar_completed_at": bars[-1].completed_at.isoformat(),
                })
        decisions = derive_strategy_001_decisions(tuple(all_bars))
        option_units: dict[str, list[StagedProviderUnit]] = {}
        for symbol in SYMBOLS:
            values = []
            for decision in (item for item in decisions if item.symbol == symbol):
                unit = option_unit(symbol, decision.signal_at)
                metadata = checkpoint_store.load_metadata(unit)
                values.append(materialize_checkpoint(
                    checkpoint_store,
                    unit,
                    metadata,
                    workspace,
                    mounted_bucket_root=MOUNT_ROOT,
                ))
            if not values:
                raise ValueError(f"frozen decision plan is empty for {symbol}")
            option_units[symbol] = values
        return (
            instrument_ids,
            tuple(all_bars),
            decisions,
            tuple(streams),
            option_units,
            tuple(raw_descriptors),
        )
    finally:
        engine.dispose()


def _chunk_object(predecessor_sha256: str, symbol: str, ordinal: int, unit_key: str) -> str:
    return (
        f"{PREFIX}/normalization/chunks/{predecessor_sha256}/"
        f"{symbol}/{ordinal:05d}-{unit_key}.json"
    )


def run_stage_2(
    store: DurableObjectStore,
    checkpoint_store: GCSCheckpointStore,
    database_url: str,
    workspace: Path,
    progress: Progress,
    predecessor: Receipt,
) -> Receipt:
    """Normalize in rollback-only canonical sessions and durably seal exact bytes."""
    validate_receipt(store, predecessor, expected_stage=1, predecessor=None)
    started = time.perf_counter()
    progress.event("FINALIZATION_STAGE_STARTED", 2, started)
    calendar = SessionCalendarResolver()
    discoveries, liquidity = restore_stage_1_indexes(store, predecessor, workspace / "stage-2")
    engine = create_engine(database_url)
    try:
        instrument_ids, _all_bars, decisions, stream_plan, option_units, underlying_raw = (
            _load_only_units(checkpoint_store, workspace, calendar, database_url)
        )
        outputs: list[dict[str, Any]] = []
        chunk_plan: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in SYMBOLS}
        accounting_values = []
        option_streams: list[dict[str, Any]] = []

        with Session(engine, expire_on_commit=False) as session:
            transaction = session.begin()
            try:
                instruments = {symbol: _instrument(session, symbol) for symbol in SYMBOLS}
                normalizer = DataNormalizer(session, calendar)
                completed_units = 0
                processed_unit_bytes = 0
                for offset, symbol in enumerate(SYMBOLS, start=len(stream_plan)):
                    gate = HistoricalOptionEnrollmentGate(session)
                    symbol_accounting = []
                    for batch in discoveries[symbol].enrollment_batches():
                        outcome = gate.enroll(
                            batch,
                            underlying_instrument_id=instruments[symbol].instrument_id,
                            underlying_symbol=symbol,
                            research_replay_mode=True,
                        )
                        discoveries[symbol].record_accepted(outcome.accepted_contract_keys)
                        symbol_accounting.append(outcome.accounting)
                    accounting_values.append(CanonicalResolutionAccounting.combine(symbol_accounting))

                    prior: datetime | None = None
                    count = 0
                    first: datetime | None = None
                    last: datetime | None = None
                    chunk_paths: list[Path] = []
                    for ordinal, unit in enumerate(sorted(
                        option_units[symbol],
                        key=lambda item: item.signal_at or datetime.min.replace(tzinfo=timezone.utc),
                    )):
                        object_name = _chunk_object(predecessor.sha256, symbol, ordinal, unit.unit_key)
                        chunk_path = workspace / "stage-2" / "chunks" / symbol / f"{ordinal:05d}.json"
                        existing = store.stat(object_name)
                        if existing is not None:
                            if not existing.sha256:
                                raise ValueError("durable normalization chunk lacks SHA-256 metadata")
                            store.download(object_name, chunk_path)
                            digest, size = sha256_file(chunk_path)
                            if (digest, size) != (existing.sha256, existing.byte_count):
                                raise ValueError("durable normalization chunk is corrupt")
                            snapshots = tuple(
                                CanonicalOptionChainSnapshot.model_validate(item)
                                for item in iter_canonical_json_array(chunk_path)
                            )
                        else:
                            snapshots = normalize_staged_option_unit(
                                unit,
                                normalizer=normalizer,
                                underlying_instrument_id=instruments[symbol].instrument_id,
                                symbol=symbol,
                                discovery_spool=discoveries[symbol],
                                liquidity_index=liquidity[symbol],
                            )
                            with CanonicalJsonArrayWriter(chunk_path) as writer:
                                for snapshot in snapshots:
                                    writer.append(snapshot)
                            descriptor = _upload_file(
                                store,
                                object_name,
                                chunk_path,
                                "application/json",
                                symbol=symbol,
                                unit_key=unit.unit_key,
                                ordinal=ordinal,
                            )
                            existing = ObjectIdentity(**descriptor["identity"])
                        for snapshot in snapshots:
                            if prior is not None and snapshot.canonical_completed_at <= prior:
                                raise ValueError("staged option snapshots are not strictly chronological")
                            prior = snapshot.canonical_completed_at
                            first = first or prior
                            last = prior
                            count += 1
                        chunk_paths.append(chunk_path)
                        chunk_plan[symbol].append({
                            "object_name": object_name,
                            "identity": _identity(existing),
                            "unit_key": unit.unit_key,
                            "ordinal": ordinal,
                        })
                        completed_units += 1
                        processed_unit_bytes += unit.artifact.byte_size
                        release_unit_memory()
                        if completed_units % 25 == 0:
                            elapsed = max(time.perf_counter() - started, 0.001)
                            progress.event(
                                "FINALIZATION_STAGE_PROGRESS",
                                2,
                                started,
                                unit_counts=completed_units,
                                total_units=sum(len(value) for value in option_units.values()),
                                symbol=symbol,
                                bytes_processed=processed_unit_bytes,
                                throughput_mib_s=round(
                                    processed_unit_bytes / (1024 * 1024) / elapsed, 3
                                ),
                            )
                    if not count or first is None or last is None:
                        raise ValueError(f"no canonical option snapshots were staged for {symbol}")
                    normalized_path = workspace / "stage-2" / f"{symbol}-options.json"
                    with CanonicalJsonArrayWriter(normalized_path) as writer:
                        for chunk_path in chunk_paths:
                            for item in iter_canonical_json_array(chunk_path):
                                writer.append(item)
                    option_streams.append({
                        "instrument_id": str(instruments[symbol].instrument_id),
                        "symbol": symbol,
                        "stream_role": str(StreamRole.OPTION_CHAIN_QUOTES),
                        "stream_ordinal": offset,
                        "normalized_path": str(normalized_path),
                        "bar_count": count,
                        "first_bar_start_at": first.isoformat(),
                        "last_bar_completed_at": last.isoformat(),
                    })
            finally:
                transaction.rollback()

        for stream in (*stream_plan, *option_streams):
            path = Path(stream["normalized_path"])
            kind = "bars" if stream["stream_role"] == str(StreamRole.UNDERLYING_SIGNAL_BARS) else "options"
            descriptor = _upload_file(
                store,
                f"{PREFIX}/normalization/{predecessor.sha256}/{stream['symbol']}-{kind}.json",
                path,
                "application/json",
                symbol=stream["symbol"],
                stream_role=stream["stream_role"],
            )
            stream["normalized_object"] = descriptor
            outputs.append(descriptor)

        for symbol in SYMBOLS:
            discoveries[symbol].commit()
            discoveries[symbol].connection.execute("VACUUM")
            discoveries[symbol].commit()
            outputs.append(_upload_file(
                store,
                f"{PREFIX}/normalization/{predecessor.sha256}/{symbol}-accepted.sqlite3",
                discoveries[symbol].path,
                "application/vnd.sqlite3",
                symbol=symbol,
                index_kind="accepted_discoveries",
            ))

        plan = {
            "plan_version": NORMALIZATION_STAGE_VERSION,
            "instrument_ids": {key: str(value) for key, value in instrument_ids.items()},
            "decisions": [item.model_dump(mode="json") for item in decisions],
            "streams": [
                {key: value for key, value in stream.items() if key != "normalized_path"}
                for stream in (*stream_plan, *option_streams)
            ],
            "underlying_raw": list(underlying_raw),
            "option_raw": list(predecessor.inputs),
            "resolution_accounting": CanonicalResolutionAccounting.combine(
                accounting_values
            ).model_dump(mode="json"),
            "chunks": chunk_plan,
        }
        raw_by_key = {
            (item["symbol"], item.get("stream_role", str(StreamRole.OPTION_CHAIN_QUOTES))): item
            for item in (*underlying_raw, *predecessor.inputs)
        }
        manifest_streams = []
        for stream in plan["streams"]:
            raw_descriptor = raw_by_key[(stream["symbol"], stream["stream_role"])]
            normalized_descriptor = stream["normalized_object"]
            manifest_streams.append({
                "instrument_id": stream["instrument_id"],
                "symbol": stream["symbol"],
                "stream_role": stream["stream_role"],
                "stream_ordinal": stream["stream_ordinal"],
                "raw_content_sha256": raw_descriptor["identity"]["sha256"],
                "normalized_content_sha256": normalized_descriptor["identity"]["sha256"],
                "bar_count": stream["bar_count"],
                "first_bar_start_at": stream["first_bar_start_at"],
                "last_bar_completed_at": stream["last_bar_completed_at"],
            })
        _, expected_dataset_hash = HistoricalDatasetRegistry.build_manifest(
            dataset_name=f"THETA-PILOT-{AUTHORIZED_START.isoformat()}-{AUTHORIZED_END.isoformat()}",
            provider_name="THETA_DATA",
            calendar_version=calendar.calendar_version,
            normalization_policy_version="NORM-PILOT-CORPUS-v1",
            streams=tuple(manifest_streams),
        )
        normalized_paths = {
            (stream["symbol"], stream["stream_role"]): Path(stream["normalized_path"])
            for stream in (*stream_plan, *option_streams)
        }
        with Session(engine) as qualification_session:
            expected_qualification = qualify_staged(
                CorpusQualificationEngine(qualification_session, calendar),
                StagedCorpusQualificationInput(
                    provider_code="THETA_DATA",
                    start_session=AUTHORIZED_START,
                    end_session=AUTHORIZED_END,
                    symbols=SYMBOLS,
                    bar_artifacts=tuple(
                        staged_artifact(
                            normalized_paths[(symbol, str(StreamRole.UNDERLYING_SIGNAL_BARS))],
                            "application/json",
                        )
                        for symbol in SYMBOLS
                    ),
                    option_snapshot_artifacts=tuple(
                        staged_artifact(
                            normalized_paths[(symbol, str(StreamRole.OPTION_CHAIN_QUOTES))],
                            "application/json",
                        )
                        for symbol in SYMBOLS
                    ),
                    decision_points=decisions,
                    raw_artifact_sha256s=tuple(
                        item["raw_content_sha256"] for item in sorted(
                            manifest_streams, key=lambda value: value["stream_ordinal"]
                        )
                    ),
                    normalized_dataset_manifest_sha256=expected_dataset_hash,
                    resolution_accounting=CanonicalResolutionAccounting.model_validate(
                        plan["resolution_accounting"]
                    ),
                ),
            )
        plan["expected_dataset_manifest_sha256"] = expected_dataset_hash
        plan["expected_qualification"] = expected_qualification.model_dump(mode="json")
        plan_path = workspace / "stage-2" / "plan.json"
        _write_bytes(plan_path, json.dumps(
            plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8"))
        outputs.append(_upload_file(
            store,
            f"{PREFIX}/normalization/{predecessor.sha256}/plan.json",
            plan_path,
            "application/json",
            artifact_kind="stage_2_plan",
        ))
        receipt = Receipt(
            receipt_version=RECEIPT_VERSION,
            dataset=DATASET,
            stage=2,
            stage_version=NORMALIZATION_STAGE_VERSION,
            predecessor_sha256=predecessor.sha256,
            inputs=tuple((*predecessor.inputs, *underlying_raw)),
            outputs=tuple(outputs),
            facts={
                "unit_count": sum(len(value) for value in option_units.values()),
                "decision_count": len(decisions),
                "resolution_accounting": plan["resolution_accounting"],
            },
        )
        seal_receipt(store, receipt)
        progress.event("FINALIZATION_STAGE_COMPLETED", 2, started, receipt_sha256=receipt.sha256)
        return receipt
    finally:
        for resource in (*discoveries.values(), *liquidity.values()):
            resource.__exit__(None, None, None)
        engine.dispose()


def _stage_output(receipt: Receipt, **matches: Any) -> dict[str, Any]:
    values = [
        item for item in receipt.outputs
        if all(item.get(key) == value for key, value in matches.items())
    ]
    if len(values) != 1:
        raise ValueError(f"receipt output cardinality mismatch: {matches}")
    return values[0]


def _restore_plan(store: DurableObjectStore, receipt: Receipt, workspace: Path) -> dict[str, Any]:
    descriptor = _stage_output(receipt, artifact_kind="stage_2_plan")
    path = workspace / "stage-3" / "plan.json"
    restore_verified_object(store, descriptor, path)
    try:
        plan = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Stage 2 plan is invalid") from error
    if plan.get("plan_version") != NORMALIZATION_STAGE_VERSION:
        raise ValueError("Stage 2 plan version mismatch")
    return plan


def _authority_exists(session: Session, plan: dict[str, Any]) -> bool:
    dataset = session.scalar(select(HistoricalMarketDataset).where(
        HistoricalMarketDataset.dataset_manifest_sha256
        == plan["expected_dataset_manifest_sha256"]
    ))
    expected_bytes = json.dumps(
        plan["expected_qualification"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    qualification_hash = hashlib.sha256(expected_bytes).hexdigest()
    artifact = session.scalar(select(HistoricalMarketArtifact).where(
        HistoricalMarketArtifact.content_sha256 == qualification_hash
    ))
    if (dataset is None) != (artifact is None):
        raise ValueError("partial canonical authority contradicts Stage 2 plan")
    if artifact is not None and (
        artifact.artifact_role != "NORMALIZED_RESEARCH_STREAM"
        or artifact.byte_size != len(expected_bytes)
    ):
        raise ValueError("qualification authority provenance is invalid")
    return dataset is not None


def run_stage_3(
    store: DurableObjectStore,
    database_url: str,
    workspace: Path,
    progress: Progress,
    stage_1: Receipt,
    predecessor: Receipt,
) -> Receipt:
    """Create canonical authority in one transaction, then seal its durable receipt."""
    validate_receipt(store, stage_1, expected_stage=1, predecessor=None)
    validate_receipt(store, predecessor, expected_stage=2, predecessor=stage_1)
    started = time.perf_counter()
    progress.event("FINALIZATION_STAGE_STARTED", 3, started)
    plan = _restore_plan(store, predecessor, workspace)
    engine = create_engine(database_url)
    calendar = SessionCalendarResolver()
    discoveries: dict[str, OptionDiscoverySpool] = {}
    try:
        for symbol in SYMBOLS:
            descriptor = _stage_output(
                predecessor, symbol=symbol, index_kind="accepted_discoveries"
            )
            path = workspace / "stage-3" / f"{symbol}-accepted.sqlite3"
            restore_verified_object(store, descriptor, path)
            discoveries[symbol] = OptionDiscoverySpool(path, symbol)

        staged_streams = []
        raw_descriptors = {
            (item["symbol"], item.get("stream_role", str(StreamRole.OPTION_CHAIN_QUOTES))): item
            for item in (*plan["underlying_raw"], *plan["option_raw"])
        }
        for stream in sorted(plan["streams"], key=lambda item: item["stream_ordinal"]):
            normalized_descriptor = stream["normalized_object"]
            normalized_path = workspace / "stage-3" / (
                f"{stream['stream_ordinal']}-{stream['symbol']}-normalized.json"
            )
            restore_verified_object(store, normalized_descriptor, normalized_path)
            raw_descriptor = raw_descriptors[(stream["symbol"], stream["stream_role"])]
            raw_path = MOUNT_ROOT / raw_descriptor["object_name"]
            if not raw_path.is_file():
                raise FileNotFoundError(f"canonical raw source is absent: {raw_path}")
            raw_identity = ObjectIdentity(**raw_descriptor["identity"])
            staged_streams.append({
                "instrument_id": stream["instrument_id"],
                "symbol": stream["symbol"],
                "stream_role": StreamRole(stream["stream_role"]),
                "stream_ordinal": stream["stream_ordinal"],
                "raw_artifact": StagedArtifact(
                    path=raw_path,
                    content_sha256=raw_identity.sha256,
                    byte_size=raw_identity.byte_count,
                    mime_type=ThetaDecodedArtifactSerializer.MIME_TYPE,
                ),
                "normalized_artifact": StagedArtifact(
                    path=normalized_path,
                    content_sha256=normalized_descriptor["identity"]["sha256"],
                    byte_size=normalized_descriptor["identity"]["byte_count"],
                    mime_type="application/json",
                ),
                "bar_count": stream["bar_count"],
                "first_bar_start_at": datetime.fromisoformat(stream["first_bar_start_at"]),
                "last_bar_completed_at": datetime.fromisoformat(stream["last_bar_completed_at"]),
            })

        expected_qualification = plan["expected_qualification"]
        expected_manifest_bytes = json.dumps(
            expected_qualification, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        with Session(engine) as check_session:
            already_committed = _authority_exists(check_session, plan)

        if not already_committed:
            with Session(engine, expire_on_commit=False) as session, session.begin():
                instruments = {symbol: _instrument(session, symbol) for symbol in SYMBOLS}
                actual_accounting = []
                for symbol in SYMBOLS:
                    gate = HistoricalOptionEnrollmentGate(session)
                    values = []
                    for batch in discoveries[symbol].enrollment_batches():
                        outcome = gate.enroll(
                            batch,
                            underlying_instrument_id=instruments[symbol].instrument_id,
                            underlying_symbol=symbol,
                            research_replay_mode=True,
                        )
                        values.append(outcome.accounting)
                    actual_accounting.append(CanonicalResolutionAccounting.combine(values))
                combined = CanonicalResolutionAccounting.combine(actual_accounting)
                if combined != CanonicalResolutionAccounting.model_validate(
                    plan["resolution_accounting"]
                ):
                    raise ValueError("Stage 3 enrollment diverges from staged equivalence gate")

                registry = HistoricalDatasetRegistry(session, STORAGE_ROOT)
                ingested_at = datetime.now(timezone.utc)
                dataset = register_staged_dataset(
                    registry,
                    dataset_name=f"THETA-PILOT-{AUTHORIZED_START.isoformat()}-{AUTHORIZED_END.isoformat()}",
                    provider_name="THETA_DATA",
                    bar_interval_seconds=60,
                    source_timezone="America/New_York",
                    source_timestamp_convention="INTERVAL_BEGIN",
                    liquidity_fidelity_tier="TIER_1_QUOTE_DEPTH",
                    price_adjustment_mode="RAW_UNADJUSTED",
                    adjustment_policy_version=None,
                    normalization_policy_version="NORM-PILOT-CORPUS-v1",
                    ingested_at=ingested_at,
                    streams=tuple(staged_streams),
                    calendar=calendar,
                )
                if dataset.dataset_manifest_sha256 != plan["expected_dataset_manifest_sha256"]:
                    raise ValueError("Stage 3 dataset identity diverges from Stage 2")
                manifest = qualify_staged(
                    CorpusQualificationEngine(session, calendar),
                    StagedCorpusQualificationInput(
                        provider_code="THETA_DATA",
                        start_session=AUTHORIZED_START,
                        end_session=AUTHORIZED_END,
                        symbols=SYMBOLS,
                        bar_artifacts=tuple(
                            item["normalized_artifact"] for item in staged_streams
                            if item["stream_role"] is StreamRole.UNDERLYING_SIGNAL_BARS
                        ),
                        option_snapshot_artifacts=tuple(
                            item["normalized_artifact"] for item in staged_streams
                            if item["stream_role"] is StreamRole.OPTION_CHAIN_QUOTES
                        ),
                        decision_points=tuple(
                            PilotDecisionPoint.model_validate(item) for item in plan["decisions"]
                        ),
                        raw_artifact_sha256s=tuple(
                            item["raw_artifact"].content_sha256 for item in staged_streams
                        ),
                        normalized_dataset_manifest_sha256=dataset.dataset_manifest_sha256,
                        resolution_accounting=combined,
                    ),
                )
                if manifest.canonical_bytes() != expected_manifest_bytes:
                    raise ValueError("Stage 3 qualification diverges from Stage 2 equivalence gate")
                CorpusQualificationEngine(
                    session, calendar
                ).persist_manifest(registry, manifest, created_at=ingested_at)
            progress.event(
                "FINALIZATION_STAGE_PROGRESS",
                3,
                started,
                event_detail="canonical_transaction_committed",
            )

        with Session(engine) as check_session:
            if not _authority_exists(check_session, plan):
                raise ValueError("canonical authority is absent after Stage 3 commit")
        receipt = Receipt(
            receipt_version=RECEIPT_VERSION,
            dataset=DATASET,
            stage=3,
            stage_version=AUTHORITY_STAGE_VERSION,
            predecessor_sha256=predecessor.sha256,
            inputs=predecessor.outputs,
            outputs=(),
            facts={
                "dataset_manifest_sha256": plan["expected_dataset_manifest_sha256"],
                "qualification": expected_qualification,
                "qualification_manifest_bytes_sha256": hashlib.sha256(
                    expected_manifest_bytes
                ).hexdigest(),
            },
        )
        seal_receipt(store, receipt)
        progress.event("FINALIZATION_STAGE_COMPLETED", 3, started, receipt_sha256=receipt.sha256)
        return receipt
    finally:
        for resource in discoveries.values():
            resource.__exit__(None, None, None)
        engine.dispose()


def run_stage_4(
    store: DurableObjectStore,
    database_url: str,
    workspace: Path,
    progress: Progress,
    stage_2: Receipt,
    predecessor: Receipt,
) -> Receipt:
    """Revalidate canonical authority, seal identical bytes, and audit population."""
    stage_1 = load_receipt(store, 1)
    if stage_1 is None:
        raise ValueError("Stage 1 receipt is absent")
    validate_receipt(store, stage_1, expected_stage=1, predecessor=None)
    validate_receipt(store, stage_2, expected_stage=2, predecessor=stage_1)
    validate_receipt(store, predecessor, expected_stage=3, predecessor=stage_2)
    started = time.perf_counter()
    progress.event("FINALIZATION_STAGE_STARTED", 4, started)
    plan = _restore_plan(store, stage_2, workspace)
    expected_qualification = predecessor.facts["qualification"]
    if expected_qualification != plan["expected_qualification"]:
        raise ValueError("Stage 3 qualification contradicts Stage 2")
    manifest_bytes = json.dumps(
        expected_qualification,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if hashlib.sha256(manifest_bytes).hexdigest() != predecessor.facts[
        "qualification_manifest_bytes_sha256"
    ]:
        raise ValueError("Stage 3 manifest byte identity is invalid")

    engine = create_engine(database_url)
    accepted_spools: list[OptionDiscoverySpool] = []
    try:
        with Session(engine) as session:
            if not _authority_exists(session, plan):
                raise ValueError("Stage 3 canonical database authority is absent")
            dataset = session.scalar(select(HistoricalMarketDataset).where(
                HistoricalMarketDataset.dataset_manifest_sha256
                == plan["expected_dataset_manifest_sha256"]
            ))
            assert dataset is not None
            rows = session.scalars(select(HistoricalMarketDatasetSymbol).where(
                HistoricalMarketDatasetSymbol.dataset_id == dataset.dataset_id
            ).order_by(HistoricalMarketDatasetSymbol.stream_ordinal)).all()
            expected_streams = sorted(plan["streams"], key=lambda item: item["stream_ordinal"])
            if len(rows) != len(expected_streams):
                raise ValueError("canonical dataset stream population is incomplete")
            for row, expected in zip(rows, expected_streams, strict=True):
                raw_descriptors = {
                    (item["symbol"], item.get("stream_role", str(StreamRole.OPTION_CHAIN_QUOTES))): item
                    for item in (*plan["underlying_raw"], *plan["option_raw"])
                }
                raw = raw_descriptors[(expected["symbol"], expected["stream_role"])]
                normalized = expected["normalized_object"]
                if (
                    row.symbol != expected["symbol"]
                    or row.stream_role != expected["stream_role"]
                    or row.stream_ordinal != expected["stream_ordinal"]
                    or row.raw_content_sha256 != raw["identity"]["sha256"]
                    or row.normalized_content_sha256 != normalized["identity"]["sha256"]
                    or row.bar_count != expected["bar_count"]
                    or row.first_bar_start_at != datetime.fromisoformat(expected["first_bar_start_at"])
                    or row.last_bar_completed_at != datetime.fromisoformat(expected["last_bar_completed_at"])
                ):
                    raise ValueError("canonical population/hash/coverage audit failed")
            option_count = session.scalar(select(func.count()).select_from(Instrument).where(
                Instrument.asset_class == "OPTION",
                Instrument.underlying_symbol.in_(SYMBOLS),
                Instrument.retired_at.is_(None),
            ))
            required_options = plan["resolution_accounting"]["resolved_contracts_count"]
            if option_count is None or option_count < required_options:
                raise ValueError("canonical option enrollment population is incomplete")
            audited_options = 0
            for symbol in SYMBOLS:
                descriptor = _stage_output(
                    stage_2, symbol=symbol, index_kind="accepted_discoveries"
                )
                accepted_path = workspace / "stage-4" / f"{symbol}-accepted.sqlite3"
                restore_verified_object(store, descriptor, accepted_path)
                spool = OptionDiscoverySpool(accepted_path, symbol)
                accepted_spools.append(spool)
                gate = HistoricalOptionEnrollmentGate(session)
                for key in spool.accepted_keys():
                    expiration, strike, right = key
                    instrument_id = deterministic_option_instrument_id(
                        symbol, expiration, strike, right
                    )
                    row = session.get(Instrument, instrument_id)
                    if row is None:
                        raise ValueError("accepted canonical option identity is absent")
                    gate._validate_existing(
                        row,
                        instrument_id,
                        canonical_occ_symbol(symbol, expiration, strike, right),
                        symbol,
                        key,
                    )
                    audited_options += 1
            if audited_options != required_options:
                raise ValueError("accepted option population differs from resolution accounting")

        store.seal_bytes(
            "manifests/theta_q1_2024_manifest.json",
            manifest_bytes,
            content_type="application/vnd.kairo.corpus-qualification+json",
        )
        manifest_identity = store.stat("manifests/theta_q1_2024_manifest.json")
        if manifest_identity is None:
            raise ValueError("canonical qualification manifest seal is absent")
        receipt = Receipt(
            receipt_version=RECEIPT_VERSION,
            dataset=DATASET,
            stage=4,
            stage_version=MANIFEST_STAGE_VERSION,
            predecessor_sha256=predecessor.sha256,
            inputs=predecessor.inputs,
            outputs=({
                "object_name": "manifests/theta_q1_2024_manifest.json",
                "identity": _identity(ObjectIdentity(
                    uri=manifest_identity.uri,
                    generation=manifest_identity.generation,
                    metageneration=manifest_identity.metageneration,
                    byte_count=len(manifest_bytes),
                    sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                )),
                "artifact_kind": "canonical_qualification_manifest",
            },),
            facts={
                "dataset_manifest_sha256": plan["expected_dataset_manifest_sha256"],
                "qualification_manifest_sha256": expected_qualification[
                    "qualification_manifest_sha256"
                ],
                "stream_count": len(expected_streams),
                "active_option_count": option_count,
                "audited_option_count": audited_options,
            },
        )
        seal_receipt(store, receipt)
        progress.event("FINALIZATION_STAGE_COMPLETED", 4, started, receipt_sha256=receipt.sha256)
        return receipt
    finally:
        for spool in accepted_spools:
            spool.__exit__(None, None, None)
        engine.dispose()
