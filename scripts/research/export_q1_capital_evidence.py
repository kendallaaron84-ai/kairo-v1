"""Export a sealed capital-replay bundle from canonical normalized Q1 artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = ROOT / "backend"
for import_root in (ROOT, BACKEND_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from app.domain.enums import OptionRight, OrderSide  # noqa: E402
from app.domain.instruments import CanonicalInstrument  # noqa: E402
from app.db.models.historical import (  # noqa: E402
    HistoricalMarketArtifact,
    HistoricalMarketDataset,
    HistoricalMarketDatasetSymbol,
)
from engine.data.corpus_qualifier import (  # noqa: E402
    CorpusQualificationManifest,
    QualificationStatus,
)
from engine.data.option_enrollment import deterministic_option_instrument_id  # noqa: E402
from engine.data.streaming_pilot import iter_canonical_json_array  # noqa: E402
from engine.data.theta_v3 import (  # noqa: E402
    ThetaDecodedArtifactReader,
    ThetaDecodedArtifactSerializer,
)
from engine.strategy.ema_cross_strategy import (  # noqa: E402
    EASTERN,
    EMACrossStrategy,
    StrategyContract,
    StrategyPosition,
)
from engine.strategy.option_resolver import (  # noqa: E402
    LegacySessionExpirationResolver,
    MappingInstrumentLookup,
    OptionContractCandidate,
    resolve_legacy_option,
)
from engine.validation.models import (  # noqa: E402
    CanonicalMarketBar,
    CanonicalOptionChainSnapshot,
    CanonicalOptionContractQuote,
    StreamRole,
)
from scripts.research.run_q1_capital_matrix import (  # noqa: E402
    FORBIDDEN_SCRATCH,
    Q1CapitalMatrixEvidence,
    Q1_END,
    Q1_RTH_MINUTES,
    Q1_SESSION_COUNT,
    Q1_START,
    ArtifactReference,
    EmpiricalSignal,
    IntraTradeQuote,
    _local_path,
    verify_qualification_manifest_identity,
    verified_bytes,
)


SYMBOLS = ("TQQQ", "SQQQ")
SYMBOL_ORDER = {symbol: index for index, symbol in enumerate(SYMBOLS)}
STREAM_ROLES = (
    StreamRole.UNDERLYING_SIGNAL_BARS.value,
    StreamRole.OPTION_CHAIN_QUOTES.value,
)
QUOTE_TIMESTAMP_CONVENTION = "THETA_LAST_QUOTE_AT_INTERVAL_TIMESTAMP"


@dataclass(frozen=True)
class CanonicalStreamArtifacts:
    raw: ArtifactReference
    normalized: ArtifactReference


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _artifact_uri(root: str, content_sha256: str) -> str:
    if FORBIDDEN_SCRATCH in root:
        raise ValueError("active scratch/staging paths cannot supply canonical evidence")
    suffix = f"{content_sha256[:2]}/{content_sha256[2:4]}/{content_sha256}.json"
    parsed = urlparse(root)
    if parsed.scheme in ("gs", "https", "file"):
        return root.rstrip("/") + "/" + suffix
    return str(Path(root) / content_sha256[:2] / content_sha256[2:4] / f"{content_sha256}.json")


def _load_canonical_array(uri: str, digest: str) -> tuple[bytes, list[dict]]:
    content = verified_bytes(uri, digest)
    value = json.loads(content)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("normalized canonical artifact must contain a JSON object array")
    if _canonical_bytes(value) != content:
        raise ValueError("normalized canonical artifact bytes are not canonical JSON")
    return content, value


def _file_identity(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class CanonicalOptionSnapshotCursor:
    """Bounded reader for one immutable, chronologically ordered option stream."""

    def __init__(
        self,
        reference: ArtifactReference,
        *,
        expected_count: int,
        expected_symbol: str,
        expected_instrument_id: str,
    ) -> None:
        path = _local_path(reference.uri)
        if path is None:
            raise ValueError(
                "canonical option artifacts must use the mounted file authority"
            )
        actual_hash, actual_size = _file_identity(path)
        if (
            actual_hash != reference.content_sha256
            or actual_size != reference.byte_size
        ):
            raise ValueError("canonical option artifact SHA-256 or byte size is invalid")
        self.reference = reference
        self.expected_count = expected_count
        self.expected_symbol = expected_symbol
        self.expected_instrument_id = expected_instrument_id
        self._items: Iterator[dict[str, Any]] = iter_canonical_json_array(path)
        self._lookahead: CanonicalOptionChainSnapshot | None = None
        self._prior_at: datetime | None = None
        self._count = 0
        self._canonical_digest = hashlib.sha256(b"[")
        self._finished = False

    def _read_one(self) -> CanonicalOptionChainSnapshot | None:
        try:
            value = next(self._items)
        except StopIteration:
            return None
        snapshot = CanonicalOptionChainSnapshot.model_validate(value)
        if (
            snapshot.underlying_symbol != self.expected_symbol
            or str(snapshot.underlying_instrument_id) != self.expected_instrument_id
        ):
            raise ValueError("canonical option identity conflicts with dataset stream")
        observed_at = snapshot.canonical_completed_at
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("option snapshot timestamp must be timezone-aware")
        session = observed_at.astimezone(EASTERN).date()
        if not Q1_START <= session <= Q1_END:
            raise ValueError("canonical option snapshot lies outside certified Q1")
        if self._prior_at is not None and observed_at <= self._prior_at:
            raise ValueError("canonical option snapshots are not strictly chronological")
        if self._count:
            self._canonical_digest.update(b",")
        self._canonical_digest.update(_canonical_bytes(snapshot.model_dump(mode="json")))
        self._prior_at = observed_at
        self._count += 1
        return snapshot

    def at(self, timestamp: datetime) -> CanonicalOptionChainSnapshot | None:
        if self._finished:
            raise RuntimeError("canonical option cursor is already finalized")
        if self._lookahead is None:
            self._lookahead = self._read_one()
        while (
            self._lookahead is not None
            and self._lookahead.canonical_completed_at < timestamp
        ):
            self._lookahead = self._read_one()
        if (
            self._lookahead is not None
            and self._lookahead.canonical_completed_at == timestamp
        ):
            result = self._lookahead
            self._lookahead = None
            return result
        return None

    def finish(self) -> None:
        if self._finished:
            return
        if self._lookahead is not None:
            self._lookahead = None
        while self._read_one() is not None:
            pass
        self._canonical_digest.update(b"]")
        if self._count != self.expected_count:
            raise ValueError("normalized stream count conflicts with dataset manifest")
        if self._canonical_digest.hexdigest() != self.reference.content_sha256:
            raise ValueError("normalized option artifact bytes are not canonical JSON")
        self._finished = True


class ThetaOptionAggregateCausalReader:
    """Read-only frame index and causal quote-path reader for one binary aggregate.

    Theta's v3 quote contract defines an interval quote as the last quote at the
    returned interval timestamp. The timestamp is therefore used as the causal
    observation instant without adding a minute or treating it as a bar close.
    """

    def __init__(self, reference: ArtifactReference, *, symbol: str) -> None:
        path = _local_path(reference.uri)
        if path is None:
            raise ValueError("canonical binary aggregate must use mounted file authority")
        actual_hash, actual_size = _file_identity(path)
        if actual_hash != reference.content_sha256 or actual_size != reference.byte_size:
            raise ValueError("canonical binary aggregate SHA-256 or byte size is invalid")
        self.reference = reference
        self.path = path
        self.symbol = symbol
        self._reader = ThetaDecodedArtifactReader()
        self._quote_frames: dict[date, list[tuple[int, int]]] = defaultdict(list)
        self._index_frames()

    @staticmethod
    def _read_frame(stream) -> tuple[int, int, bytes] | None:
        prefix = stream.read(8)
        if not prefix:
            return None
        if len(prefix) != 8:
            raise ValueError("Theta aggregate frame prefix is truncated")
        length = struct.unpack(">Q", prefix)[0]
        if length <= 0:
            raise ValueError("Theta aggregate contains an empty frame")
        offset = stream.tell()
        frame = stream.read(length)
        if len(frame) != length:
            raise ValueError("Theta aggregate frame is truncated")
        if _canonical_bytes(json.loads(frame)) != frame:
            raise ValueError("Theta aggregate frame is not canonical JSON")
        return offset, length, frame

    def _index_frames(self) -> None:
        serializer = ThetaDecodedArtifactSerializer
        with self.path.open("rb") as stream:
            if stream.read(len(serializer.MAGIC)) != serializer.MAGIC:
                raise ValueError("Theta aggregate framing mismatch")
            header_frame = self._read_frame(stream)
            if header_frame is None:
                raise ValueError("Theta aggregate header is absent")
            header = json.loads(header_frame[2])
            expected_count = header.get("section_count")
            if not isinstance(expected_count, int) or expected_count < 0:
                raise ValueError("Theta aggregate section count is invalid")
            self._reader._header(header, expected_count)
            prior: bytes | None = None
            count = 0
            while framed := self._read_frame(stream):
                offset, length, frame = framed
                if prior is not None and frame < prior:
                    raise ValueError("Theta aggregate sections are not canonically ordered")
                prior = frame
                value = json.loads(frame)
                if not isinstance(value, dict) or set(value) != {
                    "endpoint", "parameters", "fields", "rows"
                }:
                    raise ValueError("Theta aggregate section schema mismatch")
                if value["endpoint"] == "option_history_quote":
                    parameters = self._reader._mapping(value["parameters"])
                    if parameters.get("symbol") != self.symbol:
                        raise ValueError("Theta aggregate quote symbol mismatch")
                    session = parameters.get("date")
                    if not isinstance(session, date):
                        raise ValueError("Theta aggregate quote session is invalid")
                    self._quote_frames[session].append((offset, length))
                count += 1
            if count != expected_count:
                raise ValueError("Theta aggregate section count mismatch")

    @staticmethod
    def _right(value: object) -> OptionRight:
        normalized = str(value).upper()
        if normalized in {"CALL", "C"}:
            return OptionRight.CALL
        if normalized in {"PUT", "P"}:
            return OptionRight.PUT
        raise ValueError("Theta aggregate option right is invalid")

    def quotes_for_contracts(
        self,
        *,
        session: date,
        contract_ids: frozenset[object],
    ) -> dict[object, tuple[IntraTradeQuote, ...]]:
        selected: dict[object, dict[datetime, IntraTradeQuote]] = {
            instrument_id: {} for instrument_id in contract_ids
        }
        with self.path.open("rb") as stream:
            for offset, length in self._quote_frames.get(session, ()):
                stream.seek(offset)
                frame = stream.read(length)
                if len(frame) != length:
                    raise ValueError("Theta aggregate indexed frame is truncated")
                section = self._reader._section(json.loads(frame))
                expiration = section.parameters.get("expiration")
                if not isinstance(expiration, date):
                    raise ValueError("Theta aggregate quote expiration is invalid")
                for row in section.records:
                    right = self._right(row.get("right"))
                    strike = Decimal(str(row["strike"]))
                    instrument_id = deterministic_option_instrument_id(
                        self.symbol, expiration, strike, right
                    )
                    if instrument_id not in selected:
                        continue
                    observed_at = row.get("timestamp")
                    if (
                        not isinstance(observed_at, datetime)
                        or observed_at.tzinfo is None
                        or observed_at.astimezone(EASTERN).date() != session
                    ):
                        raise ValueError("Theta aggregate quote timestamp is invalid")
                    quote = IntraTradeQuote(
                        timestamp=observed_at,
                        bid=Decimal(str(row["bid"])),
                        ask=Decimal(str(row["ask"])),
                    )
                    prior = selected[instrument_id].get(observed_at)
                    if prior is not None and prior != quote:
                        raise ValueError("Theta aggregate contains conflicting quote samples")
                    selected[instrument_id][observed_at] = quote
        return {
            instrument_id: tuple(quotes[timestamp] for timestamp in sorted(quotes))
            for instrument_id, quotes in selected.items()
        }


def _qualification(uri: str, digest: str) -> CorpusQualificationManifest:
    content = verified_bytes(uri, digest)
    manifest = CorpusQualificationManifest.model_validate_json(content)
    verify_qualification_manifest_identity(manifest)
    if manifest.provider_code != "THETA_DATA":
        raise ValueError("qualification manifest provider is not THETA_DATA")
    if manifest.qualification_policy_version != "CORPUS-QUALIFICATION-v1":
        raise ValueError("qualification policy version is not frozen Q1 authority")
    if (
        manifest.pilot_window.start_session,
        manifest.pilot_window.end_session,
    ) != (Q1_START, Q1_END):
        raise ValueError("qualification manifest does not cover exact Q1 2024")
    if (
        manifest.pilot_window.total_calendar_sessions != Q1_SESSION_COUNT
        or manifest.pilot_window.rth_expected_minutes != int(Q1_RTH_MINUTES)
    ):
        raise ValueError("qualification manifest Q1 session envelope is invalid")
    if manifest.overall_qualification_verdict is not QualificationStatus.PASS:
        raise ValueError("qualification manifest verdict must be PASS")
    return manifest


def _validate_dataset_manifest(body: object) -> None:
    if not isinstance(body, dict):
        raise ValueError("dataset manifest must be a JSON object")
    if body.get("provider_name") != "THETA_DATA":
        raise ValueError("dataset provider is not THETA_DATA")
    if (
        body.get("replay_mode") != "RESEARCH_REPLAY_MODE"
        or body.get("exact_prototype_replay") is not False
    ):
        raise ValueError("dataset replay provenance is not canonical research mode")
    streams = body.get("streams")
    if not isinstance(streams, list):
        raise ValueError("dataset manifest streams are absent")
    identities = [(item.get("symbol"), item.get("stream_role")) for item in streams]
    expected = [(symbol, role) for symbol in SYMBOLS for role in STREAM_ROLES]
    if sorted(identities) != sorted(expected):
        raise ValueError("dataset must contain exactly the TQQQ/SQQQ bar and option streams")


def _dataset_manifest(uri: str, expected_digest: str) -> dict:
    content = verified_bytes(uri, expected_digest)
    body = json.loads(content)
    if _canonical_bytes(body) != content:
        raise ValueError("dataset manifest is not canonical JSON")
    _validate_dataset_manifest(body)
    return body


def _dataset_from_authority_rows(
    dataset: Any,
    entries: Sequence[Any],
    artifacts: Mapping[object, Any],
) -> tuple[dict, dict[int, CanonicalStreamArtifacts]]:
    manifest_streams = []
    references: dict[int, CanonicalStreamArtifacts] = {}
    for entry in sorted(entries, key=lambda item: item.stream_ordinal):
        raw = artifacts.get(entry.raw_artifact_id)
        normalized = artifacts.get(entry.normalized_artifact_id)
        if (
            raw is None
            or raw.artifact_role != "RAW_PROVIDER_PAYLOAD"
            or raw.content_sha256 != entry.raw_content_sha256
        ):
            raise ValueError("raw provider artifact lineage does not resolve")
        if (
            normalized is None
            or normalized.artifact_role != "NORMALIZED_RESEARCH_STREAM"
            or normalized.content_sha256 != entry.normalized_content_sha256
            or normalized.mime_type != "application/json"
        ):
            raise ValueError("normalized canonical artifact lineage does not resolve")
        references[entry.stream_ordinal] = CanonicalStreamArtifacts(
            raw=ArtifactReference(
                uri=raw.storage_uri,
                content_sha256=raw.content_sha256,
                byte_size=raw.byte_size,
            ),
            normalized=ArtifactReference(
                uri=normalized.storage_uri,
                content_sha256=normalized.content_sha256,
                byte_size=normalized.byte_size,
            ),
        )
        manifest_streams.append({
            "instrument_id": str(entry.instrument_id),
            "symbol": entry.symbol,
            "stream_role": entry.stream_role,
            "stream_ordinal": entry.stream_ordinal,
            "raw_content_sha256": entry.raw_content_sha256,
            "normalized_content_sha256": entry.normalized_content_sha256,
            "bar_count": entry.bar_count,
            "first_bar_start_at": entry.first_bar_start_at.isoformat(),
            "last_bar_completed_at": entry.last_bar_completed_at.isoformat(),
        })
    body = {
        "dataset_name": dataset.dataset_name,
        "provider_name": dataset.provider_name,
        "replay_mode": "RESEARCH_REPLAY_MODE",
        "exact_prototype_replay": False,
        "calendar_version": dataset.calendar_version,
        "normalization_policy_version": dataset.normalization_policy_version,
        "streams": manifest_streams,
    }
    if hashlib.sha256(_canonical_bytes(body)).hexdigest() != dataset.dataset_manifest_sha256:
        raise ValueError("database dataset rows do not reconstruct the canonical manifest")
    _validate_dataset_manifest(body)
    return body, references


def _database_dataset(
    database_url: str,
    expected_digest: str,
) -> tuple[dict, dict[int, CanonicalStreamArtifacts]]:
    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            with Session(bind=connection) as session:
                dataset = session.scalar(select(HistoricalMarketDataset).where(
                    HistoricalMarketDataset.dataset_manifest_sha256 == expected_digest
                ))
                if dataset is None:
                    raise ValueError("qualified dataset is absent from canonical authority")
                entries = tuple(session.scalars(
                    select(HistoricalMarketDatasetSymbol)
                    .where(HistoricalMarketDatasetSymbol.dataset_id == dataset.dataset_id)
                    .order_by(HistoricalMarketDatasetSymbol.stream_ordinal)
                ))
                artifact_ids = {
                    identifier
                    for entry in entries
                    for identifier in (entry.raw_artifact_id, entry.normalized_artifact_id)
                }
                artifact_rows = tuple(session.scalars(
                    select(HistoricalMarketArtifact).where(
                        HistoricalMarketArtifact.artifact_id.in_(artifact_ids)
                    )
                ))
                return _dataset_from_authority_rows(
                    dataset,
                    entries,
                    {item.artifact_id: item for item in artifact_rows},
                )
    finally:
        engine.dispose()


def _quote_candidate(quote: CanonicalOptionContractQuote) -> OptionContractCandidate:
    return OptionContractCandidate(
        instrument_id=quote.contract_instrument_id,
        underlying_symbol=quote.underlying_symbol,
        expiration_date=quote.expiration_date,
        strike_price=quote.strike_price,
        option_right=quote.option_right,
        contract_symbol=quote.canonical_contract_symbol,
        contract_multiplier=quote.contract_multiplier,
        listing_type=quote.listing_type,
        bid=quote.bid_price,
        ask=quote.ask_price,
        volume=quote.volume,
        open_interest=quote.open_interest,
    )


def _instrument(quote: CanonicalOptionContractQuote) -> CanonicalInstrument:
    return CanonicalInstrument(
        instrument_id=quote.contract_instrument_id,
        symbol=quote.canonical_contract_symbol,
        asset_class="OPTION",
        underlying_symbol=quote.underlying_symbol,
        contract_symbol=quote.canonical_contract_symbol,
        expiration_date=quote.expiration_date,
        strike_price=quote.strike_price,
        option_right=quote.option_right,
        contract_multiplier=quote.contract_multiplier,
        listing_type=quote.listing_type,
        effective_from=datetime(1970, 1, 1, tzinfo=UTC),
    )


def _contract_for_right(
    snapshot: CanonicalOptionChainSnapshot | None,
    *,
    right: OptionRight,
    spot: Decimal,
    expiration_resolver: LegacySessionExpirationResolver,
) -> tuple[StrategyContract | None, CanonicalOptionContractQuote | None]:
    if snapshot is None:
        return None, None
    candidates = tuple(_quote_candidate(item) for item in snapshot.contracts)
    expirations = tuple(item.expiration_date for item in candidates)
    if not expirations:
        return None, None
    expiration = expiration_resolver.resolve(snapshot.underlying_symbol, expirations)
    canonical = tuple(_instrument(item) for item in snapshot.contracts)
    resolved = resolve_legacy_option(
        candidates=candidates,
        underlying_symbol=snapshot.underlying_symbol,
        expiration_date=expiration,
        option_right=right,
        spot_price=spot,
        canonical_lookup=MappingInstrumentLookup(canonical),
    )
    if resolved is None:
        return None, None
    quote = next(
        item for item in snapshot.contracts
        if item.contract_instrument_id == resolved.instrument_id
    )
    return StrategyContract(
        instrument_id=resolved.instrument_id,
        underlying_symbol=resolved.underlying_symbol,
        option_right=resolved.option_right,
        bid=resolved.bid,
        ask=resolved.ask,
        contract_multiplier=resolved.contract_multiplier,
    ), quote


@dataclass(frozen=True)
class EntryPlan:
    bar: CanonicalMarketBar
    quote: CanonicalOptionContractQuote


def _entry_plans(
    bars_by_session: Mapping[date, list[CanonicalMarketBar]],
    snapshot_cursors: Mapping[str, CanonicalOptionSnapshotCursor],
) -> tuple[EntryPlan, ...]:
    plans: list[EntryPlan] = []
    for session_date in sorted(bars_by_session):
        strategy = EMACrossStrategy(settled_cash=Decimal("1000000"))
        expiration_resolvers = {
            symbol: LegacySessionExpirationResolver(session_date=session_date)
            for symbol in SYMBOLS
        }
        for bar in sorted(
            bars_by_session[session_date],
            key=lambda item: (item.completed_at, SYMBOL_ORDER[item.symbol]),
        ):
            snapshot = snapshot_cursors[bar.symbol].at(bar.completed_at)
            call, call_quote = _contract_for_right(
                snapshot,
                right=OptionRight.CALL,
                spot=bar.close,
                expiration_resolver=expiration_resolvers[bar.symbol],
            )
            put, put_quote = _contract_for_right(
                snapshot,
                right=OptionRight.PUT,
                spot=bar.close,
                expiration_resolver=expiration_resolvers[bar.symbol],
            )
            order = strategy.on_bar(
                symbol=bar.symbol,
                close=bar.close,
                timestamp=bar.completed_at,
                call_contract=call,
                put_contract=put,
            )
            if order is None:
                if strategy.last_missing_execution_evidence == "ENTRY_OPTION_QUOTE_MISSING":
                    raise ValueError("Strategy 001 entry signal lacks canonical option evidence")
                continue
            if order.side is not OrderSide.BUY:
                raise ValueError("independent entry discovery emitted a non-entry order")
            selected = call_quote if order.option_right is OptionRight.CALL else put_quote
            if selected is None or selected.contract_instrument_id != order.instrument_id:
                raise ValueError("resolved Strategy 001 entry quote is absent")
            plans.append(EntryPlan(bar=bar, quote=selected))
    for cursor in snapshot_cursors.values():
        cursor.finish()
    return tuple(plans)


def _replay_entry(
    plan: EntryPlan,
    session_bars: Sequence[CanonicalMarketBar],
    quotes: Sequence[IntraTradeQuote],
    *,
    dataset_sha256: str,
) -> EmpiricalSignal:
    quote_stream = iter(quotes)
    quote = next(quote_stream, None)
    while quote is not None and quote.timestamp < plan.bar.completed_at:
        quote = next(quote_stream, None)
    if quote is None or quote.timestamp != plan.bar.completed_at:
        raise ValueError("binary aggregate lacks an exact T0 option quote")
    entry_quote = quote
    if (
        entry_quote.bid != plan.quote.bid_price
        or entry_quote.ask != plan.quote.ask_price
    ):
        raise ValueError("binary T0 quote conflicts with normalized decision snapshot")
    strategy = EMACrossStrategy(settled_cash=Decimal("1000000"))
    path: list[IntraTradeQuote] = []
    opened = False
    for bar in session_bars:
        if bar.completed_at < plan.bar.completed_at:
            strategy.on_bar(
                symbol=bar.symbol, close=bar.close, timestamp=bar.completed_at
            )
            continue
        if quote is None or quote.timestamp != bar.completed_at:
            raise ValueError(
                "binary aggregate quote path is incomplete at "
                f"{bar.completed_at.isoformat()}"
            )
        path.append(quote)
        if not opened:
            contract = StrategyContract(
                instrument_id=plan.quote.contract_instrument_id,
                underlying_symbol=bar.symbol,
                option_right=plan.quote.option_right,
                bid=entry_quote.bid,
                ask=entry_quote.ask,
                contract_multiplier=plan.quote.contract_multiplier,
            )
            order = strategy.on_bar(
                symbol=bar.symbol,
                close=bar.close,
                timestamp=bar.completed_at,
                call_contract=(contract if contract.option_right is OptionRight.CALL else None),
                put_contract=(contract if contract.option_right is OptionRight.PUT else None),
            )
            if order is None or order.side is not OrderSide.BUY:
                raise ValueError("frozen Strategy 001 did not reproduce the planned entry")
            strategy.record_open(StrategyPosition(
                instrument_id=contract.instrument_id,
                underlying_symbol=bar.symbol,
                option_right=contract.option_right,
                quantity=Decimal("1"),
                entry_price=entry_quote.ask,
                contract_multiplier=contract.contract_multiplier,
            ))
            opened = True
            quote = next(quote_stream, None)
            continue
        order = strategy.on_bar(
            symbol=bar.symbol,
            close=bar.close,
            timestamp=bar.completed_at,
            position_quote_bid=quote.bid,
        )
        if order is None:
            quote = next(quote_stream, None)
            continue
        if order.side is not OrderSide.SELL:
            raise ValueError("frozen Strategy 001 emitted an invalid exit order")
        identity = (
            f"{dataset_sha256}|{bar.symbol}|{plan.bar.completed_at.isoformat()}|"
            f"{plan.quote.contract_instrument_id}"
        )
        return EmpiricalSignal(
            signal_id=str(uuid5(NAMESPACE_URL, f"kairo:q1-capital-signal:{identity}")),
            contract_instrument_id=plan.quote.contract_instrument_id,
            symbol=bar.symbol,
            option_right=plan.quote.option_right,
            session=plan.bar.completed_at.astimezone(EASTERN).date(),
            signal_at=plan.bar.completed_at,
            exit_at=bar.completed_at,
            entry_bid=entry_quote.bid,
            entry_ask=entry_quote.ask,
            exit_bid=quote.bid,
            exit_ask=quote.ask,
            contract_multiplier=plan.quote.contract_multiplier,
            exit_reason=order.reason,
            intra_trade_path=tuple(path),
        )
    raise ValueError("binary aggregate path ends before a frozen Strategy 001 exit")


def extract_signals(
    bars: tuple[CanonicalMarketBar, ...],
    snapshot_cursors: Mapping[str, CanonicalOptionSnapshotCursor],
    aggregate_readers: Mapping[str, ThetaOptionAggregateCausalReader],
    *,
    dataset_sha256: str,
) -> tuple[EmpiricalSignal, ...]:
    bars_by_session: dict[date, list[CanonicalMarketBar]] = defaultdict(list)
    for bar in bars:
        if bar.completed_at.tzinfo is None:
            raise ValueError("canonical bar timestamp must be timezone-aware")
        session = bar.completed_at.astimezone(EASTERN).date()
        if not Q1_START <= session <= Q1_END:
            raise ValueError("canonical bar lies outside certified Q1")
        bars_by_session[session].append(bar)
    plans = _entry_plans(bars_by_session, snapshot_cursors)
    outcomes: list[EmpiricalSignal] = []
    plans_by_session: dict[date, list[EntryPlan]] = defaultdict(list)
    for plan in plans:
        plans_by_session[plan.bar.completed_at.astimezone(EASTERN).date()].append(plan)
    for session_date in sorted(plans_by_session):
        for symbol in SYMBOLS:
            symbol_plans = [
                plan for plan in plans_by_session[session_date]
                if plan.bar.symbol == symbol
            ]
            if not symbol_plans:
                continue
            paths = aggregate_readers[symbol].quotes_for_contracts(
                session=session_date,
                contract_ids=frozenset(
                    plan.quote.contract_instrument_id for plan in symbol_plans
                ),
            )
            symbol_bars = sorted(
                (
                    bar for bar in bars_by_session[session_date]
                    if bar.symbol == symbol
                ),
                key=lambda item: item.completed_at,
            )
            for plan in symbol_plans:
                outcomes.append(_replay_entry(
                    plan,
                    symbol_bars,
                    paths[plan.quote.contract_instrument_id],
                    dataset_sha256=dataset_sha256,
                ))
    return tuple(sorted(outcomes, key=lambda item: (item.signal_at, item.signal_id)))


def export_evidence(
    *,
    qualification_manifest_uri: str,
    qualification_manifest_sha256: str,
    dataset_manifest_uri: str | None = None,
    artifact_root: str | None = None,
    option_aggregate_root: str | None = None,
    database_url: str | None = None,
) -> Q1CapitalMatrixEvidence:
    manifest = _qualification(
        qualification_manifest_uri, qualification_manifest_sha256
    )
    dataset_sha256 = manifest.normalized_dataset_manifest_sha256
    if database_url is not None:
        if (
            dataset_manifest_uri is not None
            or artifact_root is not None
            or option_aggregate_root is not None
        ):
            raise ValueError("database and offline dataset sources are mutually exclusive")
        dataset, authority_references = _database_dataset(database_url, dataset_sha256)
    else:
        if (
            dataset_manifest_uri is None
            or artifact_root is None
            or option_aggregate_root is None
        ):
            raise ValueError(
                "offline export requires dataset manifest, normalized, and aggregate roots"
            )
        dataset = _dataset_manifest(dataset_manifest_uri, dataset_sha256)
        authority_references = None
    bars: list[CanonicalMarketBar] = []
    snapshot_cursors: dict[str, CanonicalOptionSnapshotCursor] = {}
    aggregate_readers: dict[str, ThetaOptionAggregateCausalReader] = {}
    references: list[ArtifactReference] = []
    for stream in sorted(dataset["streams"], key=lambda item: item["stream_ordinal"]):
        digest = stream.get("normalized_content_sha256")
        if not isinstance(digest, str):
            raise ValueError("normalized stream content identity is absent")
        if authority_references is not None:
            stream_artifacts = authority_references[stream["stream_ordinal"]]
            reference = stream_artifacts.normalized
        else:
            uri = _artifact_uri(artifact_root, digest)
            path = _local_path(uri)
            if path is None or not path.is_file():
                raise ValueError("offline canonical artifact does not resolve locally")
            reference = ArtifactReference(
                uri=uri,
                content_sha256=digest,
                byte_size=path.stat().st_size,
            )
        references.append(reference)
        if stream["stream_role"] == StreamRole.UNDERLYING_SIGNAL_BARS.value:
            content, rows = _load_canonical_array(reference.uri, digest)
            if len(content) != reference.byte_size:
                raise ValueError(
                    "normalized canonical artifact byte size conflicts with authority"
                )
            if len(rows) != stream.get("bar_count"):
                raise ValueError("normalized stream count conflicts with dataset manifest")
            parsed_bars = tuple(CanonicalMarketBar.model_validate(item) for item in rows)
            if any(
                item.symbol != stream["symbol"]
                or str(item.instrument_id) != stream["instrument_id"]
                for item in parsed_bars
            ):
                raise ValueError("canonical bar identity conflicts with dataset stream")
            bars.extend(parsed_bars)
        else:
            if stream["symbol"] in snapshot_cursors:
                raise ValueError("duplicate canonical option stream for symbol")
            snapshot_cursors[stream["symbol"]] = CanonicalOptionSnapshotCursor(
                reference,
                expected_count=stream["bar_count"],
                expected_symbol=stream["symbol"],
                expected_instrument_id=stream["instrument_id"],
            )
            if authority_references is not None:
                raw_reference = stream_artifacts.raw
            else:
                aggregate_path = Path(option_aggregate_root) / (
                    f"{stream['symbol']}-options.bin"
                )
                if not aggregate_path.is_file():
                    raise ValueError("offline canonical binary aggregate is absent")
                raw_reference = ArtifactReference(
                    uri=str(aggregate_path),
                    content_sha256=stream["raw_content_sha256"],
                    byte_size=aggregate_path.stat().st_size,
                )
            references.append(raw_reference)
            aggregate_readers[stream["symbol"]] = ThetaOptionAggregateCausalReader(
                raw_reference, symbol=stream["symbol"]
            )
    if {item.symbol for item in bars} != set(SYMBOLS):
        raise ValueError("canonical bars do not cover both frozen symbols")
    for symbol in SYMBOLS:
        sessions = sorted(
            item.completed_at.astimezone(EASTERN).date()
            for item in bars if item.symbol == symbol
        )
        if not sessions or (sessions[0], sessions[-1]) != (Q1_START, Q1_END):
            raise ValueError(f"canonical bars do not span exact Q1 for {symbol}")
    if set(snapshot_cursors) != set(SYMBOLS):
        raise ValueError("canonical option streams do not cover both frozen symbols")
    if set(aggregate_readers) != set(SYMBOLS):
        raise ValueError("canonical binary aggregates do not cover both frozen symbols")
    signals = extract_signals(
        tuple(bars),
        snapshot_cursors,
        aggregate_readers,
        dataset_sha256=dataset_sha256,
    )
    if len(signals) != manifest.metrics.strategy_signal_count:
        raise ValueError("exported signal population conflicts with qualification manifest")
    draft = Q1CapitalMatrixEvidence(
        schema_version="KAIRO-Q1-CAPITAL-EVIDENCE-v1",
        evidence_payload_sha256="0" * 64,
        qualification_manifest_sha256=manifest.qualification_manifest_sha256,
        qualification_manifest_content_sha256=qualification_manifest_sha256,
        normalized_dataset_manifest_sha256=dataset_sha256,
        strategy_id="EMA-CROSS-001",
        strategy_version="1.0.0",
        start_session=Q1_START,
        end_session=Q1_END,
        artifacts=tuple(references),
        signals=signals,
    )
    return draft.model_copy(update={
        "evidence_payload_sha256": hashlib.sha256(
            draft.canonical_payload_bytes()
        ).hexdigest()
    })


def evidence_bytes(evidence: Q1CapitalMatrixEvidence) -> bytes:
    evidence.verify_self_seal()
    return _canonical_bytes(evidence.model_dump(mode="json"))


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qualification-manifest-uri", required=True)
    parser.add_argument("--qualification-manifest-sha256", required=True)
    parser.add_argument("--database-url")
    parser.add_argument("--dataset-manifest-uri")
    parser.add_argument("--artifact-root")
    parser.add_argument("--option-aggregate-root")
    parser.add_argument("--output", type=Path, default=Path("q1_capital_evidence.json"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = arguments(argv)
    evidence = export_evidence(
        qualification_manifest_uri=args.qualification_manifest_uri,
        qualification_manifest_sha256=args.qualification_manifest_sha256,
        dataset_manifest_uri=args.dataset_manifest_uri,
        artifact_root=args.artifact_root,
        option_aggregate_root=args.option_aggregate_root,
        database_url=args.database_url,
    )
    content = evidence_bytes(evidence)
    _atomic_write(args.output, content)
    print(json.dumps({
        "output": str(args.output),
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "evidence_payload_sha256": evidence.evidence_payload_sha256,
        "signals": len(evidence.signals),
    }, sort_keys=True, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
