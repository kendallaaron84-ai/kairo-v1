import csv
import hashlib
import io
import json
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
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
        zone = ZoneInfo(source_timezone)
        observations = []
        for row in csv.DictReader(io.StringIO(raw_csv.decode("utf-8"))):
            observed = datetime.fromisoformat(row["timestamp"])
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=zone)
            observations.append({**row, "timestamp": observed})
        return self.normalize_typed_bars(
            observations,
            instrument_id=instrument_id,
            symbol=symbol,
            timestamp_convention=timestamp_convention,
            bar_interval_seconds=bar_interval_seconds,
        )

    def normalize_typed_bars(
        self, observations: Any, *, instrument_id: UUID, symbol: str,
        timestamp_convention: SourceTimestampConvention,
        bar_interval_seconds: int,
    ) -> tuple[CanonicalMarketBar, ...]:
        """Normalize typed provider observations without a text serialization shim."""
        instrument = self._instrument(instrument_id)
        if instrument.symbol != symbol:
            raise ValueError("bar symbol does not match canonical instrument")
        if bar_interval_seconds <= 0:
            raise ValueError("bar interval must be positive")
        result: list[CanonicalMarketBar] = []
        for row in observations:
            observed = row["timestamp"]
            if not isinstance(observed, datetime) or observed.tzinfo is None:
                raise ValueError("typed bar timestamp must be timezone-aware")
            if timestamp_convention is SourceTimestampConvention.INTERVAL_BEGIN:
                start, completed = observed, observed + timedelta(seconds=bar_interval_seconds)
            elif timestamp_convention is SourceTimestampConvention.INTERVAL_END:
                start, completed = observed - timedelta(seconds=bar_interval_seconds), observed
            else:
                raise ValueError("bar streams cannot use TICK_ARRIVAL timestamp convention")
            start, completed = start.astimezone(timezone.utc), completed.astimezone(timezone.utc)
            if not self.calendar.contains_completed_interval(start, completed):
                continue
            result.append(CanonicalMarketBar(
                instrument_id=instrument_id, symbol=symbol, interval_start_at=start,
                completed_at=completed, open=Decimal(str(row["open"])), high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])), close=Decimal(str(row["close"])),
                volume=Decimal(str(row["volume"])) if row.get("volume") not in (None, "") else None,
            ))
        self._chronological([item.completed_at for item in result])
        return tuple(result)

    def normalize_theta_bars(
        self, sections: Any, *, instrument_id: UUID, symbol: str,
    ) -> tuple[CanonicalMarketBar, ...]:
        """Own the decoded Theta-to-canonical bar boundary with no text intermediary."""
        observations = []
        observed_timestamps: set[datetime] = set()
        for section in sections:
            if section.endpoint != "stock_history_ohlc":
                continue
            for row in section.records:
                observed = row.get("timestamp")
                if not isinstance(observed, datetime) or observed.tzinfo is None:
                    raise ValueError(
                        "Theta stock OHLC timestamp must be timezone-aware"
                    )
                timestamp_utc = observed.astimezone(timezone.utc)
                if timestamp_utc in observed_timestamps:
                    raise ValueError(
                        f"Theta stock OHLC duplicate timestamp for {symbol}: "
                        f"{timestamp_utc.isoformat()}"
                    )
                observed_timestamps.add(timestamp_utc)
                observations.append(row)
        if not observations:
            raise ValueError("Theta decoded artifact contains no stock OHLC observations")
        observations.sort(key=lambda row: row["timestamp"].astimezone(timezone.utc))
        return self.normalize_typed_bars(
            observations,
            instrument_id=instrument_id,
            symbol=symbol,
            timestamp_convention=SourceTimestampConvention.INTERVAL_BEGIN,
            bar_interval_seconds=60,
        )

    def normalize_typed_option_chains(
        self, snapshots: Any,
    ) -> tuple[CanonicalOptionChainSnapshot, ...]:
        """Validate already-typed canonical snapshots at the normalization boundary."""
        result = tuple(
            item if isinstance(item, CanonicalOptionChainSnapshot)
            else CanonicalOptionChainSnapshot.model_validate(item)
            for item in snapshots
        )
        for snapshot in result:
            underlying = self._instrument(snapshot.underlying_instrument_id)
            if underlying.asset_class == "OPTION" or underlying.symbol != snapshot.underlying_symbol:
                raise ValueError("option snapshot underlying identity is not canonical")
            for quote in snapshot.contracts:
                contract = self._instrument(quote.contract_instrument_id)
                if contract.asset_class != "OPTION" or contract.underlying_symbol != underlying.symbol:
                    raise ValueError("option contract and snapshot underlying differ")
                candidate = OptionContractCandidate(
                    instrument_id=contract.instrument_id,
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
                validate_candidate_identity(candidate, _canonical(contract))
        self._chronological([item.canonical_completed_at for item in result])
        return result

    def normalize_theta_option_sections(
        self, sections: Any, *, underlying_instrument_id: UUID, symbol: str,
        accepted_contract_keys: frozenset[tuple[date, Decimal, OptionRight]] | None = None,
    ) -> tuple[CanonicalOptionChainSnapshot, ...]:
        """Join decoded Theta quote/OI/volume rows into canonical typed snapshots."""
        underlying = self._instrument(underlying_instrument_id)
        if underlying.asset_class == "OPTION" or underlying.symbol != symbol:
            raise ValueError("Theta option underlying identity is not canonical")
        sections = tuple(sections)
        quote_sections = tuple(item for item in sections if item.endpoint == "option_history_quote")
        if not quote_sections:
            raise ValueError("Theta decoded artifact contains no option quote sections")
        eastern = ZoneInfo("America/New_York")
        grouped_quotes: dict[tuple[date, datetime], list[Any]] = {}
        for quote_section in quote_sections:
            request_date = self._as_date(quote_section.parameters.get("date"))
            end_time = quote_section.parameters.get("end_time")
            if not isinstance(end_time, time):
                raise ValueError("Theta option quote section lacks a typed end_time")
            decision_at = datetime.combine(request_date, end_time, eastern).astimezone(timezone.utc)
            grouped_quotes.setdefault((request_date, decision_at), []).append(quote_section)
        snapshots = []
        for (request_date, decision_at), decision_sections in sorted(grouped_quotes.items()):
            latest_quotes: dict[tuple[date, Decimal, OptionRight], Mapping[str, Any]] = {}
            for quote_section in decision_sections:
                expiry = self._as_date(quote_section.parameters.get("expiration"))
                for row in quote_section.records:
                    try:
                        key = self._theta_contract_key(row, default_expiration=expiry)
                    except (KeyError, ValueError, InvalidOperation):
                        if accepted_contract_keys is not None:
                            continue
                        raise
                    if accepted_contract_keys is not None and key not in accepted_contract_keys:
                        continue
                    observed = row.get("timestamp")
                    if not isinstance(observed, datetime) or observed.tzinfo is None:
                        raise ValueError("Theta option quote timestamp must be timezone-aware")
                    if observed.astimezone(timezone.utc) > decision_at:
                        raise ValueError("Theta option quote is later than the decision timestamp")
                    prior = latest_quotes.get(key)
                    if prior is None or observed > prior["timestamp"]:
                        latest_quotes[key] = row
            contracts = []
            for key, quote in sorted(latest_quotes.items(), key=lambda item: (item[0][0], item[0][1], item[0][2].value)):
                expiration, strike, right = key
                contract = self.session.scalar(select(Instrument).where(
                    Instrument.asset_class == "OPTION",
                    Instrument.underlying_symbol == symbol,
                    Instrument.expiration_date == expiration,
                    Instrument.strike_price == strike,
                    Instrument.option_right == right.value,
                    Instrument.retired_at.is_(None),
                ))
                if contract is None:
                    raise ValueError("Theta option contract does not resolve to canonical identity")
                open_interest = self._theta_latest_value(
                    sections, "option_history_open_interest", key, request_date, "open_interest"
                )
                volume = self._theta_volume_through(sections, key, request_date, decision_at)
                contracts.append(CanonicalOptionContractQuote(
                    contract_instrument_id=contract.instrument_id,
                    underlying_instrument_id=underlying_instrument_id,
                    underlying_symbol=symbol,
                    canonical_contract_symbol=contract.contract_symbol,
                    expiration_date=expiration,
                    strike_price=strike,
                    option_right=right,
                    contract_multiplier=contract.contract_multiplier,
                    listing_type=contract.listing_type,
                    bid_price=Decimal(str(quote["bid"])),
                    ask_price=Decimal(str(quote["ask"])),
                    bid_size=Decimal(str(quote["bid_size"])),
                    ask_size=Decimal(str(quote["ask_size"])),
                    volume=volume,
                    open_interest=open_interest,
                    liquidity_verifiable=volume is not None and open_interest is not None,
                ))
            snapshots.append(CanonicalOptionChainSnapshot(
                underlying_instrument_id=underlying_instrument_id,
                underlying_symbol=symbol,
                canonical_completed_at=decision_at,
                contracts=tuple(contracts),
            ))
        return self.normalize_typed_option_chains(sorted(
            snapshots, key=lambda item: item.canonical_completed_at
        ))

    @staticmethod
    def _as_date(value: Any) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value)[:10])

    def _theta_contract_key(
        self, row: Mapping[str, Any], *, default_expiration: date,
    ) -> tuple[date, Decimal, OptionRight]:
        raw_right = str(row["right"]).upper()
        right = OptionRight.CALL if raw_right in {"CALL", "C"} else OptionRight.PUT if raw_right in {"PUT", "P"} else None
        if right is None:
            raise ValueError("Theta option right is invalid")
        return (
            self._as_date(row.get("expiration", default_expiration)),
            Decimal(str(row["strike"])),
            right,
        )

    def _theta_latest_value(
        self, sections: tuple[Any, ...], endpoint: str,
        key: tuple[date, Decimal, OptionRight], request_date: date, field: str,
    ) -> int | None:
        matches = []
        for section in sections:
            if section.endpoint != endpoint or self._as_date(section.parameters.get("date")) != request_date:
                continue
            default_expiry = self._as_date(section.parameters.get("expiration"))
            for row in section.records:
                try:
                    observed_key = self._theta_contract_key(
                        row, default_expiration=default_expiry
                    )
                except (KeyError, ValueError, InvalidOperation):
                    continue
                if observed_key == key and row.get(field) is not None:
                    matches.append(row)
        if not matches:
            return None
        latest = max(matches, key=lambda row: row.get("timestamp") or datetime.min.replace(tzinfo=timezone.utc))
        return int(latest[field])

    def _theta_volume_through(
        self, sections: tuple[Any, ...], key: tuple[date, Decimal, OptionRight],
        request_date: date, decision_at: datetime,
    ) -> int | None:
        local_decision = decision_at.astimezone(ZoneInfo("America/New_York"))
        endpoint_windows = (
            ("option_history_trade", local_decision.time().replace(tzinfo=None), "size"),
            (
                "option_history_ohlc",
                (local_decision - timedelta(minutes=1)).time().replace(tzinfo=None),
                "volume",
            ),
        )
        for endpoint, expected_end, volume_field in endpoint_windows:
            total = 0
            found = False
            for section in sections:
                if (
                    section.endpoint != endpoint
                    or self._as_date(section.parameters.get("date")) != request_date
                    or section.parameters.get("end_time") != expected_end
                ):
                    continue
                default_expiry = self._as_date(section.parameters.get("expiration"))
                for row in section.records:
                    observed = row.get("timestamp")
                    try:
                        observed_key = self._theta_contract_key(
                            row, default_expiration=default_expiry
                        )
                    except (KeyError, ValueError, InvalidOperation):
                        continue
                    if (
                        observed_key == key
                        and isinstance(observed, datetime)
                        and observed.tzinfo is not None
                        and observed.astimezone(timezone.utc) <= decision_at
                        and row.get(volume_field) is not None
                    ):
                        total += int(row[volume_field])
                        found = True
            if found:
                return total
        return None

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
        return self.normalize_typed_option_chains(result)

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
