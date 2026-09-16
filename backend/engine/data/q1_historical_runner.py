"""Durable local orchestration for the frozen Q1 dynamic-evidence acquisition.

This module owns ordering, retries, local persistence, receipts, checkpoints, and
the canonical local manifest.  Listing, quote, slicing, and qualification semantics
remain delegated to the already verified production components.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time as clock
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from engine.data.databento_listing_provider import DatabentoListingProvider
from engine.data.dynamic_option_slicer import (
    CompletedUnderlyingBar,
    DynamicEnvelopeSlice,
    DynamicOptionEvidenceSlicer,
)
from engine.data.thetadata_quote_provider import ThetaQuoteTransportError
from engine.validation.dynamic_envelope_qualifier import ENVELOPE_SATISFIED
from engine.validation.feed_loader import canonical_json_bytes


POLICY_ID = "DYNAMIC-ACQUISITION-Q1-v1.0"
CHECKPOINT_VERSION = "Q1-DYNAMIC-CHECKPOINT-v1"
RECEIPT_VERSION = "Q1-DYNAMIC-CELL-RECEIPT-v1"
PARQUET_SCHEMA_VERSION = "Q1-DYNAMIC-INTERVAL-PARQUET-v1"
NEW_YORK = ZoneInfo("America/New_York")
Q1_SYMBOLS = ("SQQQ", "TQQQ")
Q1_SESSIONS = tuple(
    date(2024, month, day)
    for month, days in (
        (1, (2, 3, 4, 5, 8, 9, 10, 11, 12, 16, 17, 18, 19, 22, 23, 24, 25, 26, 29, 30, 31)),
        (2, (1, 2, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 20, 21, 22, 23, 26, 27, 28, 29)),
        (3, (1, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15, 18, 19, 20, 21, 22, 25, 26, 27, 28)),
    )
    for day in days
)
EXPECTED_BARS_PER_CELL = 390
FAILURE_ABORT_COUNT = 2_380
RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)

INTERVAL_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("completed_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("spot", pa.string(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
        pa.field("slice_count", pa.int32(), nullable=False),
        pa.field("evidence_json", pa.binary(), nullable=False),
    ]
)


class ResumeProvenanceError(RuntimeError):
    code = "RESUME_ABORTED_PROVENANCE_MISMATCH"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class IrrecoverableCompletenessError(RuntimeError):
    code = "EARLY_ABORT: IRRECOVERABLE_COMPLETENESS_DEFICIT"

    def __init__(self, evaluated: int, satisfied: int) -> None:
        self.evaluated = evaluated
        self.satisfied = satisfied
        self.failed = evaluated - satisfied
        super().__init__(self.code)


@dataclass(frozen=True)
class SealedUnderlyingSession:
    bars: tuple[CompletedUnderlyingBar, ...]
    source_path: Path
    expected_sha256: str


class UnderlyingSessionSource(Protocol):
    def load_session(self, symbol: str, session: date) -> SealedUnderlyingSession: ...


class QuoteProvider(Protocol):
    def acquire_quotes(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class RunnerResult:
    completed_cells: int
    cumulative_evaluated: int
    cumulative_satisfied: int
    cumulative_failed: int
    manifest_path: Path
    manifest_root_sha256: str


@dataclass
class _FailureBudget:
    evaluated: int = 0
    satisfied: int = 0

    @property
    def failed(self) -> int:
        return self.evaluated - self.satisfied

    def record(self, satisfied: bool) -> None:
        self.evaluated += 1
        self.satisfied += int(satisfied)
        if self.failed >= FAILURE_ABORT_COUNT:
            raise IrrecoverableCompletenessError(self.evaluated, self.satisfied)


class _RetryingSplitProvider:
    def __init__(
        self,
        listing_provider: DatabentoListingProvider,
        quote_provider: QuoteProvider,
        *,
        sleeper: Callable[[float], None],
    ) -> None:
        self._listing = listing_provider
        self._quotes = quote_provider
        self._sleeper = sleeper
        self.retries = 0

    def discover_listings(self, **kwargs: Any) -> Any:
        return self._listing.discover_listings(**kwargs)

    def acquire_quotes(self, **kwargs: Any) -> Any:
        for retry_number in range(len(RETRY_DELAYS_SECONDS) + 1):
            try:
                return self._quotes.acquire_quotes(**kwargs)
            except Exception as exc:
                if not _retryable(exc) or retry_number == len(RETRY_DELAYS_SECONDS):
                    raise
                self._sleeper(RETRY_DELAYS_SECONDS[retry_number])
                self.retries += 1
        raise AssertionError("unreachable")


class Q1HistoricalRunner:
    """Execute deterministic symbol-session cells into non-authoritative local scratch."""

    def __init__(
        self,
        *,
        scratch_root: str | Path,
        underlying_source: UnderlyingSessionSource,
        quote_provider: QuoteProvider,
        sessions: tuple[date, ...] = Q1_SESSIONS,
        symbols: tuple[str, ...] = Q1_SYMBOLS,
        sleeper: Callable[[float], None] = clock.sleep,
    ) -> None:
        self.root = Path(scratch_root)
        self.underlying_source = underlying_source
        self.quote_provider = quote_provider
        self.sessions = sessions
        self.symbols = tuple(symbol.upper() for symbol in symbols)
        self.sleeper = sleeper
        if not sessions or len(set(sessions)) != len(sessions):
            raise ValueError("sessions must be non-empty and unique")
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must be non-empty and unique")
        if tuple(sorted(sessions)) != sessions or tuple(sorted(self.symbols)) != self.symbols:
            raise ValueError("sessions and symbols must use deterministic ascending order")

    @property
    def cells(self) -> tuple[tuple[str, date], ...]:
        return tuple((symbol, session) for session in self.sessions for symbol in self.symbols)

    def run(self, *, stop_after_cells: int | None = None) -> RunnerResult | None:
        self.root.mkdir(parents=True, exist_ok=True)
        checkpoint = self._load_and_verify_checkpoint()
        completed = list(checkpoint["completed_cells"])
        budget = _FailureBudget(
            evaluated=int(checkpoint["cumulative_evaluated"]),
            satisfied=int(checkpoint["cumulative_satisfied"]),
        )
        processed_now = 0
        for symbol, session in self.cells[len(completed) :]:
            cell = self._process_cell(symbol, session, budget)
            completed.append(cell)
            self._write_checkpoint(completed, budget)
            processed_now += 1
            if stop_after_cells is not None and processed_now >= stop_after_cells:
                return None
        manifest_path, root_digest = write_manifest(self.root)
        return RunnerResult(
            completed_cells=len(completed),
            cumulative_evaluated=budget.evaluated,
            cumulative_satisfied=budget.satisfied,
            cumulative_failed=budget.failed,
            manifest_path=manifest_path,
            manifest_root_sha256=root_digest,
        )

    def _process_cell(
        self, symbol: str, session: date, budget: _FailureBudget
    ) -> dict[str, Any]:
        sealed = self.underlying_source.load_session(symbol, session)
        source_digest, source_size = file_identity(sealed.source_path)
        if source_digest != sealed.expected_sha256:
            raise ValueError("sealed underlying source failed SHA-256 verification")
        _validate_bars(sealed.bars, symbol, session)

        definition_path = self.root / "definitions" / symbol / f"{symbol}_definition_{session}.dbn"
        listing = DatabentoListingProvider((definition_path,))
        provider = _RetryingSplitProvider(
            listing, self.quote_provider, sleeper=self.sleeper
        )
        slicer = DynamicOptionEvidenceSlicer(provider)
        rows: list[dict[str, Any]] = []
        reason_counts: dict[str, int] = {}
        cell_satisfied = 0
        for bar in sealed.bars:
            slices = slicer.slice_bar(bar)
            satisfied = bool(slices) and all(
                item.qualification.status == ENVELOPE_SATISFIED for item in slices
            )
            budget.record(satisfied)
            cell_satisfied += int(satisfied)
            for item in slices:
                for reason in item.qualification.reason_codes:
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1
            rows.append(_interval_row(bar, slices, satisfied))

        partition = self.root / "intervals" / f"symbol={symbol}" / f"date={session:%Y%m%d}" / "intervals.parquet"
        _write_parquet(partition, rows)
        partition_sha, partition_size = file_identity(partition)
        definition_sha, definition_size = file_identity(definition_path)
        receipt_path = self.root / "receipts" / "daily" / f"receipt_{symbol}_{session:%Y-%m-%d}.json"
        receipt = {
            "receipt_version": RECEIPT_VERSION,
            "policy_id": POLICY_ID,
            "cell_id": f"{symbol}_{session:%Y-%m-%d}",
            "symbol": symbol,
            "session": session.isoformat(),
            "underlying_source": {"path": str(sealed.source_path), "sha256": source_digest, "byte_count": source_size},
            "definition_source": {"relative_path": definition_path.relative_to(self.root).as_posix(), "sha256": definition_sha, "byte_count": definition_size},
            "partition": {"relative_path": partition.relative_to(self.root).as_posix(), "sha256": partition_sha, "byte_count": partition_size, "schema_version": PARQUET_SCHEMA_VERSION},
            "interval_counts": {"evaluated": len(rows), "satisfied": cell_satisfied, "failed": len(rows) - cell_satisfied},
            "failure_reasons": dict(sorted(reason_counts.items())),
            "operational_counters": {"retries": provider.retries},
        }
        _atomic_json(receipt_path, receipt)
        receipt_sha, receipt_size = file_identity(receipt_path)
        return {
            "cell_id": receipt["cell_id"],
            "symbol": symbol,
            "session": session.isoformat(),
            "partition": {**receipt["partition"]},
            "receipt": {"relative_path": receipt_path.relative_to(self.root).as_posix(), "sha256": receipt_sha, "byte_count": receipt_size},
        }

    def _load_and_verify_checkpoint(self) -> dict[str, Any]:
        path = self.root / "state" / "checkpoint.json"
        if not path.exists():
            return _empty_checkpoint()
        try:
            checkpoint = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            raise ResumeProvenanceError("checkpoint is unreadable") from exc
        if checkpoint.get("checkpoint_version") != CHECKPOINT_VERSION:
            raise ResumeProvenanceError("checkpoint version differs")
        if checkpoint.get("policy_id") != POLICY_ID:
            raise ResumeProvenanceError("checkpoint policy identity differs")
        completed = checkpoint.get("completed_cells")
        if not isinstance(completed, list):
            raise ResumeProvenanceError("completed cell ledger is invalid")
        expected_ids = [f"{symbol}_{session:%Y-%m-%d}" for symbol, session in self.cells]
        observed_ids = [item.get("cell_id") for item in completed if isinstance(item, dict)]
        if observed_ids != expected_ids[: len(observed_ids)] or len(observed_ids) != len(completed):
            raise ResumeProvenanceError("completed cells are not an exact plan prefix")
        expected_last = observed_ids[-1] if observed_ids else None
        expected_receipt = completed[-1]["receipt"]["sha256"] if completed else None
        if (
            checkpoint.get("completed_cells_count") != len(completed)
            or checkpoint.get("last_completed_cell") != expected_last
            or checkpoint.get("cell_receipt_sha256") != expected_receipt
        ):
            raise ResumeProvenanceError("checkpoint summary contradicts cell ledger")
        for item in completed:
            for name in ("partition", "receipt"):
                identity = item.get(name, {})
                self._verify_local_identity(identity, name)
        evaluated = int(checkpoint.get("cumulative_evaluated", -1))
        satisfied = int(checkpoint.get("cumulative_satisfied", -1))
        failed = int(checkpoint.get("cumulative_failed", -1))
        if evaluated != len(completed) * EXPECTED_BARS_PER_CELL or evaluated - satisfied != failed:
            raise ResumeProvenanceError("checkpoint counters contradict completed cells")
        return checkpoint

    def _verify_local_identity(self, identity: dict[str, Any], label: str) -> None:
        try:
            relative = Path(identity["relative_path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError
            path = self.root / relative
            digest, size = file_identity(path)
            if digest != identity["sha256"] or size != int(identity["byte_count"]):
                raise ValueError
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise ResumeProvenanceError(f"{label} identity differs") from exc

    def _write_checkpoint(self, completed: list[dict[str, Any]], budget: _FailureBudget) -> None:
        payload = {
            "checkpoint_version": CHECKPOINT_VERSION,
            "policy_id": POLICY_ID,
            "last_completed_cell": completed[-1]["cell_id"] if completed else None,
            "completed_cells_count": len(completed),
            "cell_receipt_sha256": completed[-1]["receipt"]["sha256"] if completed else None,
            "cumulative_evaluated": budget.evaluated,
            "cumulative_satisfied": budget.satisfied,
            "cumulative_failed": budget.failed,
            "completed_cells": completed,
        }
        _atomic_json(self.root / "state" / "checkpoint.json", payload)


def _empty_checkpoint() -> dict[str, Any]:
    return {
        "checkpoint_version": CHECKPOINT_VERSION,
        "policy_id": POLICY_ID,
        "last_completed_cell": None,
        "completed_cells_count": 0,
        "cell_receipt_sha256": None,
        "completed_cells": [],
        "cumulative_evaluated": 0,
        "cumulative_satisfied": 0,
        "cumulative_failed": 0,
    }


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (ThetaQuoteTransportError, TimeoutError, ConnectionError)):
        return True
    return getattr(exc, "status_code", None) in {502, 503, 504}


def _validate_bars(
    bars: tuple[CompletedUnderlyingBar, ...], symbol: str, session: date
) -> None:
    if len(bars) != EXPECTED_BARS_PER_CELL:
        raise ValueError("sealed session must contain exactly 390 completed bars")
    expected = datetime.combine(session, time(9, 31), NEW_YORK)
    for index, bar in enumerate(bars):
        if bar.symbol.upper() != symbol:
            raise ValueError("underlying bar symbol contradicts cell")
        if bar.completed_at.astimezone(NEW_YORK) != expected + timedelta(minutes=index):
            raise ValueError("underlying bars must span 09:31 through 16:00 ET")


def _interval_row(
    bar: CompletedUnderlyingBar,
    slices: tuple[DynamicEnvelopeSlice, ...],
    satisfied: bool,
) -> dict[str, Any]:
    ordered = sorted(slices, key=lambda item: item.expiration_date or date.min)
    evidence = {
        "symbol": bar.symbol.upper(),
        "completed_at": bar.completed_at.astimezone(timezone.utc).isoformat(),
        "spot": format(bar.close, "f"),
        "slices": [_slice_payload(item) for item in ordered],
    }
    return {
        "schema_version": PARQUET_SCHEMA_VERSION,
        "symbol": bar.symbol.upper(),
        "completed_at": bar.completed_at.astimezone(timezone.utc),
        "spot": format(bar.close, "f"),
        "status": ENVELOPE_SATISFIED if satisfied else "DYNAMIC_ENVELOPE_DEFICIT",
        "slice_count": len(ordered),
        "evidence_json": canonical_json_bytes(evidence),
    }


def _slice_payload(item: DynamicEnvelopeSlice) -> dict[str, Any]:
    contracts = sorted(
        item.acquired_universe.contracts,
        key=lambda value: (value.strike, value.right, value.contract_id),
    )
    return {
        "expiration": item.expiration_date.isoformat() if item.expiration_date else None,
        "listing_discovery_succeeded": item.listing_discovery_succeeded,
        "quote_acquisition_succeeded": item.quote_acquisition_succeeded,
        "requested_contract_ids": list(item.requested_contract_ids),
        "qualification": {
            "status": item.qualification.status,
            "atm_strike": format(item.qualification.atm_strike, "f") if item.qualification.atm_strike is not None else None,
            "required_strikes": [format(value, "f") for value in item.qualification.required_strikes],
            "reason_codes": list(item.qualification.reason_codes),
        },
        "contracts": [
            {
                "contract_id": value.contract_id,
                "strike": format(value.strike, "f"),
                "right": value.right,
                "bid": format(value.bid, "f") if value.bid is not None else None,
                "ask": format(value.ask, "f") if value.ask is not None else None,
            }
            for value in contracts
        ],
    }


def _write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    table = pa.Table.from_pylist(rows, schema=INTERVAL_SCHEMA)
    pq.write_table(
        table,
        temporary,
        compression="snappy",
        row_group_size=EXPECTED_BARS_PER_CELL,
        use_dictionary=False,
        write_statistics=True,
    )
    temporary.replace(path)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(canonical_json_bytes(value) + b"\n")
    temporary.replace(path)


def file_identity(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def write_manifest(root: Path) -> tuple[Path, str]:
    root = Path(root)
    manifest = root / "MANIFEST.sha256"
    paths = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path != manifest and not path.name.endswith(".tmp")
    )
    records = []
    for path in paths:
        digest, size = file_identity(path)
        records.append(f"{digest} {size} {path.relative_to(root).as_posix()}\n")
    payload = "".join(records).encode("utf-8")
    temporary = manifest.with_suffix(".sha256.tmp")
    temporary.write_bytes(payload)
    temporary.replace(manifest)
    return manifest, hashlib.sha256(payload).hexdigest()
