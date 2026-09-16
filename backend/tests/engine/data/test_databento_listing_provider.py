from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from engine.data.databento_listing_provider import (
    DatabentoListedContract,
    DatabentoListingProvider,
)


UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")


def _definition(
    *,
    instrument_id: int,
    ts_event: str,
    raw_symbol: str,
    expiration: str = "2024-01-05",
    strike: str = "50",
    instrument_class: str = "C",
    action: str = "A",
    underlying: str = "TQQQ",
    activation: str | None = None,
) -> dict[str, object]:
    return {
        "publisher_id": 1,
        "instrument_id": instrument_id,
        "ts_event": ts_event,
        "raw_symbol": raw_symbol,
        "underlying": underlying,
        "expiration": expiration,
        "strike_price": strike,
        "instrument_class": instrument_class,
        "security_update_action": action,
        "activation": activation,
    }


def _write_fixture(path: Path, definitions: list[dict[str, object]]) -> bytes:
    payload = b"".join(
        json.dumps(item, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        for item in definitions
    )
    path.write_bytes(payload)
    return payload


def _query(
    provider: DatabentoListingProvider,
    clock: str,
    *,
    symbol: str = "TQQQ",
):
    completed_at = datetime.fromisoformat(f"2024-01-02T{clock}:00").replace(
        tzinfo=NEW_YORK
    )
    return provider.discover_listings(symbol=symbol, completed_at=completed_at)


def _contracts(snapshot) -> tuple[DatabentoListedContract, ...]:
    return tuple(
        contract
        for expiration in snapshot.expirations
        for contract in expiration.contracts
    )


def test_session_start_baseline_and_provenance_are_replayed(tmp_path: Path) -> None:
    fixture = tmp_path / "definitions.jsonl"
    raw = _write_fixture(
        fixture,
        [
            _definition(
                instrument_id=101,
                ts_event="2024-01-02T13:00:00Z",
                raw_symbol="TQQQ  240105C00050000",
            ),
            _definition(
                instrument_id=102,
                ts_event="2024-01-02T13:00:01Z",
                raw_symbol="TQQQ  240105P00050000",
                instrument_class="P",
            ),
        ],
    )

    provider = DatabentoListingProvider([fixture])
    snapshot = _query(provider, "09:30")

    assert snapshot.discovery_succeeded is True
    assert snapshot.timestamp == datetime(2024, 1, 2, 9, 30, tzinfo=NEW_YORK)
    assert snapshot.expirations[0].strikes == (Decimal("50"),)
    contracts = _contracts(snapshot)
    assert [contract.right for contract in contracts] == ["CALL", "PUT"]
    assert all(contract.underlying_symbol == "TQQQ" for contract in contracts)
    assert all(contract.active_listed is True for contract in contracts)
    assert contracts[0].provider_instrument_id == 101
    assert contracts[0].definition_effective_at == datetime(
        2024, 1, 2, 13, 0, tzinfo=UTC
    )
    assert provider.source_digests == ((fixture, hashlib.sha256(raw).hexdigest()),)


def test_intraday_addition_is_visible_only_at_its_effective_time(tmp_path: Path) -> None:
    fixture = tmp_path / "definitions.jsonl"
    _write_fixture(
        fixture,
        [
            _definition(
                instrument_id=101,
                ts_event="2024-01-02T13:00:00Z",
                raw_symbol="TQQQ  240105C00050000",
            ),
            _definition(
                instrument_id=103,
                ts_event="2024-01-02T16:15:00Z",
                raw_symbol="TQQQ  240105C00055000",
                strike="55",
            ),
        ],
    )
    provider = DatabentoListingProvider([fixture])

    assert [item.strike for item in _contracts(_query(provider, "11:14"))] == [
        Decimal("50")
    ]
    assert [item.strike for item in _contracts(_query(provider, "11:15"))] == [
        Decimal("50"),
        Decimal("55"),
    ]


def test_future_definition_later_in_file_never_leaks_backward(tmp_path: Path) -> None:
    fixture = tmp_path / "definitions.jsonl"
    _write_fixture(
        fixture,
        [
            _definition(
                instrument_id=201,
                ts_event="2024-01-02T19:00:00Z",
                raw_symbol="TQQQ  240105P00060000",
                strike="60",
                instrument_class="P",
            ),
            _definition(
                instrument_id=101,
                ts_event="2024-01-02T13:00:00Z",
                raw_symbol="TQQQ  240105C00050000",
            ),
        ],
    )
    provider = DatabentoListingProvider([fixture])

    assert [item.contract_id for item in _contracts(_query(provider, "10:00"))] == [
        "TQQQ  240105C00050000"
    ]


def test_call_put_rights_are_provider_defined_not_synthesized(tmp_path: Path) -> None:
    fixture = tmp_path / "definitions.jsonl"
    _write_fixture(
        fixture,
        [
            _definition(
                instrument_id=101,
                ts_event="2024-01-02T13:00:00Z",
                raw_symbol="TQQQ  240105P00050000",
                instrument_class="P",
            )
        ],
    )

    contracts = _contracts(_query(DatabentoListingProvider([fixture]), "09:30"))

    assert len(contracts) == 1
    assert contracts[0].right == "PUT"


def test_nonuniform_provider_strike_spacing_is_untouched(tmp_path: Path) -> None:
    fixture = tmp_path / "definitions.jsonl"
    strikes = ("41", "42.5", "47", "60")
    _write_fixture(
        fixture,
        [
            _definition(
                instrument_id=100 + index,
                ts_event="2024-01-02T13:00:00Z",
                raw_symbol=f"TQQQ-{strike}-C",
                strike=strike,
            )
            for index, strike in enumerate(strikes)
        ],
    )

    snapshot = _query(DatabentoListingProvider([fixture]), "09:30")

    assert snapshot.expirations[0].strikes == tuple(Decimal(value) for value in strikes)


def test_multiple_expirations_are_filtered_only_by_zero_to_five_dte(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "definitions.jsonl"
    expirations = ("2024-01-01", "2024-01-02", "2024-01-05", "2024-01-07", "2024-01-08")
    _write_fixture(
        fixture,
        [
            _definition(
                instrument_id=100 + index,
                ts_event="2024-01-02T13:00:00Z",
                raw_symbol=f"TQQQ-{expiration}-C",
                expiration=expiration,
            )
            for index, expiration in enumerate(expirations)
        ],
    )

    snapshot = _query(DatabentoListingProvider([fixture]), "09:30")

    assert [item.expiration_date.isoformat() for item in snapshot.expirations] == [
        "2024-01-02",
        "2024-01-05",
        "2024-01-07",
    ]


def test_modify_and_delete_replay_canonical_contract_state(tmp_path: Path) -> None:
    fixture = tmp_path / "definitions.jsonl"
    raw_symbol = "TQQQ  240105C00050000"
    _write_fixture(
        fixture,
        [
            _definition(
                instrument_id=101,
                ts_event="2024-01-02T13:00:00Z",
                raw_symbol=raw_symbol,
            ),
            _definition(
                instrument_id=201,
                ts_event="2024-01-02T15:00:00Z",
                raw_symbol=raw_symbol,
                action="M",
            ),
            _definition(
                instrument_id=201,
                ts_event="2024-01-02T17:00:00Z",
                raw_symbol=raw_symbol,
                action="D",
            ),
        ],
    )
    provider = DatabentoListingProvider([fixture])

    assert _contracts(_query(provider, "09:30"))[0].provider_instrument_id == 101
    assert _contracts(_query(provider, "10:00"))[0].provider_instrument_id == 201
    assert _contracts(_query(provider, "12:00")) == ()


def test_activation_time_is_enforced_without_future_leakage(tmp_path: Path) -> None:
    fixture = tmp_path / "definitions.jsonl"
    _write_fixture(
        fixture,
        [
            _definition(
                instrument_id=101,
                ts_event="2024-01-02T13:00:00Z",
                raw_symbol="TQQQ  240105C00050000",
                activation="2024-01-02T16:15:00Z",
            )
        ],
    )
    provider = DatabentoListingProvider([fixture])

    assert _contracts(_query(provider, "11:14")) == ()
    assert len(_contracts(_query(provider, "11:15"))) == 1


def test_identical_bytes_and_timestamp_are_deterministic(tmp_path: Path) -> None:
    definitions = [
        _definition(
            instrument_id=102,
            ts_event="2024-01-02T13:00:01Z",
            raw_symbol="TQQQ  240105P00050000",
            instrument_class="P",
        ),
        _definition(
            instrument_id=101,
            ts_event="2024-01-02T13:00:00Z",
            raw_symbol="TQQQ  240105C00050000",
        ),
    ]
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    raw = _write_fixture(first, definitions)
    second.write_bytes(raw)

    first_result = _query(DatabentoListingProvider([first]), "09:30")
    second_result = _query(DatabentoListingProvider([second]), "09:30")

    assert first_result == second_result


@pytest.mark.parametrize(
    "field,value",
    [("ts_event", "2024-01-02T13:00:00"), ("strike_price", "0")],
)
def test_invalid_definition_fails_closed(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    fixture = tmp_path / "definitions.jsonl"
    definition = _definition(
        instrument_id=101,
        ts_event="2024-01-02T13:00:00Z",
        raw_symbol="TQQQ  240105C00050000",
    )
    definition[field] = value
    _write_fixture(fixture, [definition])

    with pytest.raises(ValueError, match="invalid definition"):
        DatabentoListingProvider([fixture])
