"""Point-in-time OPRA definition replay for the independent listing plane.

The provider reads previously downloaded Databento ``definition`` data only.  It
does not own a Databento client and cannot request quotes, trades, or historical
data.  JSON Lines is supported for transparent synthetic fixtures and DBN/DBN.ZST
is supported for production definition files.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Literal
from zoneinfo import ZoneInfo

from engine.data.dynamic_option_slicer import (
    ProviderExpirationListing,
    ProviderListedContract,
    ProviderListingSnapshot,
)


NEW_YORK = ZoneInfo("America/New_York")
DefinitionAction = Literal["ADD", "MODIFY", "DELETE"]


@dataclass(frozen=True, kw_only=True)
class DatabentoListedContract(ProviderListedContract):
    """A provider-listed contract with its definition provenance intact."""

    underlying_symbol: str
    provider_instrument_id: int
    publisher_id: int
    definition_effective_at: datetime
    active_listed: bool = True


@dataclass(frozen=True)
class DatabentoDefinitionRecord:
    """Normalized fields required to replay an OPRA definition lifecycle."""

    publisher_id: int
    instrument_id: int
    effective_at: datetime
    raw_symbol: str
    underlying_symbol: str
    expiration_date: date
    strike: Decimal
    right: Literal["CALL", "PUT"]
    action: DefinitionAction
    activation_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.effective_at.tzinfo is None:
            raise ValueError("definition effective timestamp must be timezone-aware")
        if self.activation_at is not None and self.activation_at.tzinfo is None:
            raise ValueError("definition activation timestamp must be timezone-aware")
        if not self.raw_symbol.strip():
            raise ValueError("definition raw_symbol is required")
        if not self.underlying_symbol.strip():
            raise ValueError("definition underlying is required")
        if self.strike <= 0:
            raise ValueError("definition strike must be positive")


class DatabentoListingProvider:
    """Replay file-backed OPRA definitions as they were known at a timestamp.

    ``discover_listings`` is deliberately the only provider operation.  Quote
    acquisition belongs to a separate evidence plane.
    """

    def __init__(self, definition_files: Iterable[str | Path]) -> None:
        paths = tuple(Path(path) for path in definition_files)
        if not paths:
            raise ValueError("at least one Databento definition file is required")

        records: list[DatabentoDefinitionRecord] = []
        source_digests: list[tuple[Path, str]] = []
        for path in paths:
            if not path.is_file():
                raise ValueError(f"definition file does not exist: {path}")
            source_digests.append((path, _sha256_file(path)))
            records.extend(_load_definition_file(path))
        if not records:
            raise ValueError("definition files contain no CALL or PUT definitions")

        self._records = tuple(sorted(records, key=_record_order_key))
        self._symbols = frozenset(record.underlying_symbol for record in self._records)
        self._source_digests = tuple(source_digests)

    @property
    def source_digests(self) -> tuple[tuple[Path, str], ...]:
        """Ordered raw SHA-256 identities for the replay inputs."""

        return self._source_digests

    def discover_listings(
        self,
        *,
        symbol: str,
        completed_at: datetime,
    ) -> ProviderListingSnapshot:
        if completed_at.tzinfo is None:
            raise ValueError("completed_at must be timezone-aware")
        requested_symbol = symbol.strip().upper()
        if not requested_symbol:
            raise ValueError("symbol is required")
        if requested_symbol not in self._symbols:
            raise ValueError(
                f"definition corpus contains no records for symbol {requested_symbol}"
            )

        query_time = completed_at.astimezone(timezone.utc)
        state: dict[str, DatabentoDefinitionRecord] = {}
        for record in self._records:
            if record.effective_at > query_time:
                break
            if record.underlying_symbol != requested_symbol:
                continue
            if record.action == "DELETE":
                state.pop(record.raw_symbol, None)
            else:
                state[record.raw_symbol] = record

        session_date = completed_at.astimezone(NEW_YORK).date()
        active = tuple(
            record
            for record in state.values()
            if 0 <= (record.expiration_date - session_date).days <= 5
            and (record.activation_at is None or record.activation_at <= query_time)
        )
        by_expiration: dict[date, list[DatabentoDefinitionRecord]] = {}
        for record in active:
            by_expiration.setdefault(record.expiration_date, []).append(record)

        expirations = tuple(
            _build_expiration_listing(expiration_date, definitions)
            for expiration_date, definitions in sorted(by_expiration.items())
        )
        return ProviderListingSnapshot(
            symbol=requested_symbol,
            timestamp=completed_at,
            discovery_succeeded=True,
            expirations=expirations,
        )


def _build_expiration_listing(
    expiration_date: date,
    definitions: list[DatabentoDefinitionRecord],
) -> ProviderExpirationListing:
    ordered = sorted(
        definitions,
        key=lambda item: (item.strike, item.right, item.raw_symbol, item.instrument_id),
    )
    seen_contracts: set[str] = set()
    contracts: list[DatabentoListedContract] = []
    for item in ordered:
        if item.raw_symbol in seen_contracts:
            raise ValueError(f"duplicate active contract identity: {item.raw_symbol}")
        seen_contracts.add(item.raw_symbol)
        contracts.append(
            DatabentoListedContract(
                contract_id=item.raw_symbol,
                expiration_date=item.expiration_date,
                strike=item.strike,
                right=item.right,
                underlying_symbol=item.underlying_symbol,
                provider_instrument_id=item.instrument_id,
                publisher_id=item.publisher_id,
                definition_effective_at=item.effective_at,
                active_listed=True,
            )
        )
    return ProviderExpirationListing(
        expiration_date=expiration_date,
        strikes=tuple(sorted({item.strike for item in ordered})),
        contracts=tuple(contracts),
    )


def _record_order_key(record: DatabentoDefinitionRecord) -> tuple[Any, ...]:
    action_rank = {"ADD": 0, "MODIFY": 1, "DELETE": 2}
    return (
        record.effective_at,
        record.underlying_symbol,
        record.raw_symbol,
        record.publisher_id,
        record.instrument_id,
        action_rank[record.action],
    )


def _load_definition_file(path: Path) -> list[DatabentoDefinitionRecord]:
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        return _load_json_lines(path)
    return _load_dbn(path)


def _load_json_lines(path: Path) -> list[DatabentoDefinitionRecord]:
    records: list[DatabentoDefinitionRecord] = []
    with path.open("r", encoding="utf-8", newline="") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError("definition record must be a JSON object")
                record = _record_from_mapping(payload)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"invalid definition at {path}:{line_number}: {exc}"
                ) from exc
            if record is not None:
                records.append(record)
    return records


def _load_dbn(path: Path) -> list[DatabentoDefinitionRecord]:
    try:
        import databento as db
    except ImportError as exc:  # pragma: no cover - dependency failure is environment-specific
        raise RuntimeError("databento is required to read DBN definition files") from exc

    store = db.DBNStore.from_file(path)
    schema = str(store.schema).lower()
    if "definition" not in schema:
        raise ValueError(f"expected Databento definition schema, found {store.schema!s}")

    records: list[DatabentoDefinitionRecord] = []

    def append_record(message: Any) -> None:
        record = _record_from_dbn(message)
        if record is not None:
            records.append(record)

    store.replay(append_record)
    return records


def _record_from_dbn(message: Any) -> DatabentoDefinitionRecord | None:
    try:
        import databento_dbn as dbn
    except ImportError as exc:  # pragma: no cover - dependency failure is environment-specific
        raise RuntimeError("databento-dbn is required to decode DBN definitions") from exc

    if not isinstance(message, dbn.InstrumentDefMsg):
        raise ValueError(f"unexpected record in definition file: {type(message).__name__}")
    right = _parse_right(str(message.instrument_class))
    if right is None:
        return None
    return DatabentoDefinitionRecord(
        publisher_id=int(message.publisher_id),
        instrument_id=int(message.instrument_id),
        effective_at=_parse_timestamp_ns(int(message.ts_event), "ts_event"),
        raw_symbol=str(message.raw_symbol).strip(),
        underlying_symbol=(str(message.underlying) or str(message.asset)).strip().upper(),
        expiration_date=_parse_timestamp_ns(
            int(message.expiration), "expiration"
        ).date(),
        strike=Decimal(int(message.strike_price)) / Decimal(dbn.FIXED_PRICE_SCALE),
        right=right,
        action=_parse_action(str(message.security_update_action)),
        activation_at=_optional_timestamp_ns(int(message.activation), dbn.UNDEF_TIMESTAMP),
    )


def _record_from_mapping(payload: dict[str, Any]) -> DatabentoDefinitionRecord | None:
    right = _parse_right(str(payload["instrument_class"]))
    if right is None:
        return None
    underlying = str(payload.get("underlying") or payload.get("asset") or "").upper()
    return DatabentoDefinitionRecord(
        publisher_id=int(payload.get("publisher_id", 0)),
        instrument_id=int(payload["instrument_id"]),
        effective_at=_parse_datetime(payload["ts_event"], "ts_event"),
        raw_symbol=str(payload["raw_symbol"]).strip(),
        underlying_symbol=underlying,
        expiration_date=_parse_expiration(payload["expiration"]),
        strike=Decimal(str(payload["strike_price"])),
        right=right,
        action=_parse_action(str(payload["security_update_action"])),
        activation_at=(
            _parse_datetime(payload["activation"], "activation")
            if payload.get("activation") is not None
            else None
        ),
    )


def _parse_right(value: str) -> Literal["CALL", "PUT"] | None:
    normalized = value.strip().upper()
    if normalized in {"C", "CALL"}:
        return "CALL"
    if normalized in {"P", "PUT"}:
        return "PUT"
    return None


def _parse_action(value: str) -> DefinitionAction:
    normalized = value.strip().upper()
    actions: dict[str, DefinitionAction] = {
        "A": "ADD",
        "ADD": "ADD",
        "M": "MODIFY",
        "MODIFY": "MODIFY",
        "D": "DELETE",
        "DELETE": "DELETE",
    }
    try:
        return actions[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported security_update_action: {value!r}") from exc


def _parse_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, int):
        return _parse_timestamp_ns(value, field)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _parse_expiration(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, int):
        return _parse_timestamp_ns(value, "expiration").date()
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        return _parse_datetime(text, "expiration").date()


def _parse_timestamp_ns(value: int, field: str) -> datetime:
    if value < 0:
        raise ValueError(f"{field} nanoseconds must be non-negative")
    seconds, nanoseconds = divmod(value, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
        microsecond=nanoseconds // 1_000
    )


def _optional_timestamp_ns(value: int, undefined: int) -> datetime | None:
    return None if value == undefined else _parse_timestamp_ns(value, "activation")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
