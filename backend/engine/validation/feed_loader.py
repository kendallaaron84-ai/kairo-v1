import csv
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.configuration import Instrument
from app.db.models.historical import HistoricalMarketArtifact, HistoricalMarketDataset, HistoricalMarketDatasetSymbol
from app.domain.enums import OptionRight
from app.domain.instruments import CanonicalInstrument
from engine.intelligence.storage_driver import LocalContentAddressedStorage
from engine.strategy.option_resolver import OptionContractCandidate, validate_candidate_identity
from engine.validation.models import CanonicalMarketBar, CanonicalOptionChainSnapshot, CanonicalOptionContractQuote, DatasetManifest, SourceTimestampConvention, StreamRole
from engine.validation.session_calendar import SessionCalendarResolver


def _canonical(row: Instrument) -> CanonicalInstrument:
    return CanonicalInstrument(
        instrument_id=row.instrument_id, symbol=row.symbol, asset_class=row.asset_class,
        currency=row.currency, exchange=row.exchange, underlying_symbol=row.underlying_symbol,
        contract_symbol=row.contract_symbol, expiration_date=row.expiration_date,
        strike_price=row.strike_price, option_right=row.option_right,
        contract_multiplier=row.contract_multiplier, listing_type=row.listing_type,
        effective_from=row.effective_from, retired_at=row.retired_at,
    )


def canonical_json_bytes(value: Any) -> bytes:
    def default(item: Any) -> str:
        if isinstance(item, Decimal):
            return format(item, "f")
        if isinstance(item, (datetime, UUID)):
            return str(item)
        raise TypeError(type(item).__name__)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=default).encode("utf-8")


class DataNormalizer:
    BAR_POLICY_VERSION = "NORM-BAR-UTC-CAUSAL-v1"
    OPTION_POLICY_VERSION = "NORM-OPT-UTC-ENRICHED-v1"

    def __init__(self, session: Session, calendar: SessionCalendarResolver | None = None) -> None:
        self.session = session
        self.calendar = calendar or SessionCalendarResolver()

    def normalize_bars(
        self, raw_csv: bytes, *, instrument_id: UUID, symbol: str,
        source_timezone: str, timestamp_convention: SourceTimestampConvention,
        bar_interval_seconds: int,
    ) -> tuple[CanonicalMarketBar, ...]:
        instrument = self._instrument(instrument_id)
        if instrument.symbol != symbol:
            raise ValueError("bar symbol does not match canonical instrument")
        if bar_interval_seconds <= 0:
            raise ValueError("bar interval must be positive")
        zone = ZoneInfo(source_timezone)
        result: list[CanonicalMarketBar] = []
        for row in csv.DictReader(io.StringIO(raw_csv.decode("utf-8"))):
            observed = datetime.fromisoformat(row["timestamp"])
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=zone)
            if timestamp_convention is SourceTimestampConvention.INTERVAL_BEGIN:
                start = observed
                completed = observed + timedelta(seconds=bar_interval_seconds)
            elif timestamp_convention is SourceTimestampConvention.INTERVAL_END:
                completed = observed
                start = observed - timedelta(seconds=bar_interval_seconds)
            else:
                raise ValueError("bar streams cannot use TICK_ARRIVAL timestamp convention")
            start, completed = start.astimezone(timezone.utc), completed.astimezone(timezone.utc)
            if not self.calendar.contains_completed_interval(start, completed):
                continue
            result.append(CanonicalMarketBar(
                instrument_id=instrument_id, symbol=symbol, interval_start_at=start,
                completed_at=completed, open=Decimal(row["open"]), high=Decimal(row["high"]),
                low=Decimal(row["low"]), close=Decimal(row["close"]),
                volume=Decimal(row["volume"]) if row.get("volume") not in (None, "") else None,
            ))
        self._chronological([item.completed_at for item in result])
        return tuple(result)

    def normalize_option_chains(self, raw_json: bytes) -> tuple[CanonicalOptionChainSnapshot, ...]:
        payload = json.loads(raw_json)
        snapshots = payload.get("snapshots", payload if isinstance(payload, list) else [])
        result: list[CanonicalOptionChainSnapshot] = []
        for snapshot in snapshots:
            underlying_id = UUID(str(snapshot["underlying_instrument_id"]))
            underlying = self._instrument(underlying_id)
            if underlying.symbol != snapshot["underlying_symbol"] or underlying.asset_class == "OPTION":
                raise ValueError("option snapshot underlying identity is not canonical")
            completed = datetime.fromisoformat(snapshot["completed_at"])
            if completed.tzinfo is None:
                raise ValueError("option snapshot completion timestamp must be timezone-aware")
            contracts: list[CanonicalOptionContractQuote] = []
            for raw in snapshot["contracts"]:
                contract = self._instrument(UUID(str(raw["instrument_id"])))
                if contract.asset_class != "OPTION":
                    raise ValueError("option-chain candidate must resolve to OPTION asset class")
                candidate = OptionContractCandidate(
                    instrument_id=contract.instrument_id,
                    underlying_symbol=snapshot["underlying_symbol"],
                    expiration_date=raw["expiration_date"], strike_price=Decimal(str(raw["strike_price"])),
                    option_right=OptionRight(raw["option_right"]), contract_symbol=raw["contract_symbol"],
                    contract_multiplier=Decimal(str(raw["contract_multiplier"])), listing_type=raw["listing_type"],
                    bid=Decimal(str(raw["bid_price"])), ask=Decimal(str(raw["ask_price"])),
                    volume=raw.get("volume"), open_interest=raw.get("open_interest"),
                )
                validate_candidate_identity(candidate, _canonical(contract))
                if contract.underlying_symbol != underlying.symbol:
                    raise ValueError("option contract and snapshot underlying differ")
                contracts.append(CanonicalOptionContractQuote(
                    contract_instrument_id=contract.instrument_id, underlying_instrument_id=underlying_id,
                    underlying_symbol=underlying.symbol, canonical_contract_symbol=contract.contract_symbol,
                    expiration_date=contract.expiration_date, strike_price=contract.strike_price,
                    option_right=contract.option_right, contract_multiplier=contract.contract_multiplier,
                    listing_type=contract.listing_type, bid_price=candidate.bid, ask_price=candidate.ask,
                    bid_size=Decimal(str(raw["bid_size"])), ask_size=Decimal(str(raw["ask_size"])),
                    volume=candidate.volume, open_interest=candidate.open_interest,
                    liquidity_verifiable=candidate.volume is not None and candidate.open_interest is not None,
                ))
            result.append(CanonicalOptionChainSnapshot(
                underlying_instrument_id=underlying_id, underlying_symbol=underlying.symbol,
                canonical_completed_at=completed.astimezone(timezone.utc), contracts=tuple(contracts),
            ))
        self._chronological([item.canonical_completed_at for item in result])
        return tuple(result)

    @staticmethod
    def normalized_bytes(items: tuple[Any, ...]) -> bytes:
        return canonical_json_bytes([item.model_dump(mode="json") for item in items])

    def _instrument(self, instrument_id: UUID) -> Instrument:
        row = self.session.get(Instrument, instrument_id)
        if row is None or row.retired_at is not None:
            raise ValueError("instrument is absent or retired")
        return row

    @staticmethod
    def _chronological(values: list[datetime]) -> None:
        if any(current <= previous for previous, current in zip(values, values[1:])):
            raise ValueError("normalized observations must be strictly chronological")


class HistoricalDatasetRegistry:
    """Append-only content-addressed registry and deterministic dataset manifest builder."""

    def __init__(self, session: Session, storage_root: str | Path) -> None:
        self.session = session
        self.storage = LocalContentAddressedStorage(storage_root)

    def persist_artifact(self, content: bytes, *, role: str, mime_type: str, created_at: datetime) -> HistoricalMarketArtifact:
        digest = hashlib.sha256(content).hexdigest()
        existing = self.session.scalar(select(HistoricalMarketArtifact).where(HistoricalMarketArtifact.content_sha256 == digest))
        if existing:
            if existing.artifact_role != role or existing.byte_size != len(content):
                raise ValueError("content identity already exists with conflicting provenance")
            return existing
        artifact = HistoricalMarketArtifact(
            artifact_id=uuid5(NAMESPACE_URL, f"kairo:market-artifact:{digest}"), artifact_role=role,
            content_sha256=digest, mime_type=mime_type, byte_size=len(content),
            storage_uri=self.storage.write_bytes(digest, content, mime_type), created_at=created_at,
        )
        self.session.add(artifact)
        self.session.flush()
        return artifact

    @staticmethod
    def build_manifest(*, dataset_name: str, provider_name: str, calendar_version: str, normalization_policy_version: str, streams: tuple[dict, ...]) -> tuple[dict, str]:
        body = {
            "dataset_name": dataset_name, "provider_name": provider_name,
            "replay_mode": "RESEARCH_REPLAY_MODE", "exact_prototype_replay": False,
            "calendar_version": calendar_version,
            "normalization_policy_version": normalization_policy_version,
            "streams": sorted(streams, key=lambda item: item["stream_ordinal"]),
        }
        digest = hashlib.sha256(canonical_json_bytes(body)).hexdigest()
        return body, digest

    def register_dataset(
        self, *, dataset_name: str, provider_name: str, bar_interval_seconds: int,
        source_timezone: str, source_timestamp_convention: str,
        liquidity_fidelity_tier: str, price_adjustment_mode: str,
        adjustment_policy_version: str | None, normalization_policy_version: str,
        ingested_at: datetime, streams: tuple[dict, ...],
        calendar: SessionCalendarResolver | None = None,
    ) -> DatasetManifest:
        """Persist one deterministic immutable dataset and its dual-artifact streams.

        Each stream supplies instrument_id, symbol, stream_role, stream_ordinal,
        raw_bytes, normalized_bytes, mime types, count, and causal coverage timestamps.
        """
        authority = calendar or SessionCalendarResolver()
        manifest_streams: list[dict] = []
        artifacts: list[tuple[HistoricalMarketArtifact, HistoricalMarketArtifact]] = []
        for item in sorted(streams, key=lambda value: value["stream_ordinal"]):
            raw = self.persist_artifact(item["raw_bytes"], role="RAW_PROVIDER_PAYLOAD", mime_type=item.get("raw_mime_type", "application/octet-stream"), created_at=ingested_at)
            normalized = self.persist_artifact(item["normalized_bytes"], role="NORMALIZED_RESEARCH_STREAM", mime_type=item.get("normalized_mime_type", "application/json"), created_at=ingested_at)
            artifacts.append((raw, normalized))
            manifest_streams.append({
                "instrument_id": str(item["instrument_id"]), "symbol": item["symbol"],
                "stream_role": str(item["stream_role"]), "stream_ordinal": item["stream_ordinal"],
                "raw_content_sha256": raw.content_sha256,
                "normalized_content_sha256": normalized.content_sha256,
                "bar_count": item["bar_count"],
                "first_bar_start_at": item["first_bar_start_at"].isoformat(),
                "last_bar_completed_at": item["last_bar_completed_at"].isoformat(),
            })
        body, digest = self.build_manifest(
            dataset_name=dataset_name, provider_name=provider_name,
            calendar_version=authority.calendar_version,
            normalization_policy_version=normalization_policy_version,
            streams=tuple(manifest_streams),
        )
        dataset_id = uuid5(NAMESPACE_URL, f"kairo:historical-dataset:{digest}")
        existing = self.session.get(HistoricalMarketDataset, dataset_id)
        if existing is None:
            coverage_start = min(item["first_bar_start_at"] for item in streams)
            coverage_end = max(item["last_bar_completed_at"] for item in streams)
            existing = HistoricalMarketDataset(
                dataset_id=dataset_id, dataset_name=dataset_name, provider_name=provider_name,
                bar_interval_seconds=bar_interval_seconds, source_timezone=source_timezone,
                calendar_name=authority.calendar_name, calendar_version=authority.calendar_version,
                source_timestamp_convention=source_timestamp_convention,
                liquidity_fidelity_tier=liquidity_fidelity_tier,
                price_adjustment_mode=price_adjustment_mode,
                adjustment_policy_version=adjustment_policy_version,
                normalization_policy_version=normalization_policy_version,
                coverage_start=coverage_start, coverage_end=coverage_end,
                dataset_manifest_sha256=digest, ingested_at=ingested_at,
            )
            self.session.add(existing); self.session.flush()
            for item, (raw, normalized) in zip(sorted(streams, key=lambda value: value["stream_ordinal"]), artifacts, strict=True):
                self.session.add(HistoricalMarketDatasetSymbol(
                    symbol_entry_id=uuid5(NAMESPACE_URL, f"kairo:historical-stream:{digest}:{item['stream_ordinal']}"),
                    dataset_id=dataset_id, instrument_id=item["instrument_id"], symbol=item["symbol"],
                    stream_role=str(item["stream_role"]), stream_ordinal=item["stream_ordinal"],
                    raw_artifact_id=raw.artifact_id, raw_content_sha256=raw.content_sha256,
                    normalized_artifact_id=normalized.artifact_id, normalized_content_sha256=normalized.content_sha256,
                    bar_count=item["bar_count"], first_bar_start_at=item["first_bar_start_at"],
                    last_bar_completed_at=item["last_bar_completed_at"],
                ))
            self.session.flush()
        elif existing.dataset_manifest_sha256 != digest:
            raise ValueError("deterministic dataset identity conflicts with persisted manifest")
        return DatasetManifest(
            dataset_id=dataset_id, dataset_name=dataset_name, provider_name=provider_name,
            calendar_version=authority.calendar_version,
            normalization_policy_version=normalization_policy_version,
            streams=tuple(manifest_streams), dataset_manifest_sha256=digest,
        )
