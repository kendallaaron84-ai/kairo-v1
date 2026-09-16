from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import databento_dbn as dbn
import pytest
import zstandard

from engine.data.databento_listing_provider import (
    DatabentoListedContract,
    DatabentoListingProvider,
)


UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
DefinitionSources = dict[str, tuple[Path, bytes]]


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


def _timestamp_ns(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.timestamp()) * 1_000_000_000 + parsed.microsecond * 1_000


def _expiration_ns(value: str) -> int:
    expiration = date.fromisoformat(value)
    close = datetime.combine(expiration, time(21), tzinfo=UTC)
    return int(close.timestamp()) * 1_000_000_000


def _encode_dbn(definitions: list[dict[str, object]]) -> bytes:
    instrument_classes = {
        "C": dbn.InstrumentClass.CALL,
        "P": dbn.InstrumentClass.PUT,
    }
    update_actions = {
        "A": dbn.SecurityUpdateAction.ADD,
        "M": dbn.SecurityUpdateAction.MODIFY,
        "D": dbn.SecurityUpdateAction.DELETE,
    }
    records = []
    for item in definitions:
        event_timestamp = _timestamp_ns(str(item["ts_event"]))
        activation = item.get("activation")
        records.append(
            dbn.InstrumentDefMsg(
                publisher_id=int(item["publisher_id"]),
                instrument_id=int(item["instrument_id"]),
                ts_event=event_timestamp,
                ts_recv=event_timestamp,
                min_price_increment=1_000_000,
                display_factor=dbn.FIXED_PRICE_SCALE,
                raw_symbol=str(item["raw_symbol"]),
                asset=str(item["underlying"]),
                security_type="OPT",
                instrument_class=instrument_classes[str(item["instrument_class"])],
                security_update_action=update_actions[
                    str(item["security_update_action"])
                ],
                expiration=_expiration_ns(str(item["expiration"])),
                activation=(
                    _timestamp_ns(str(activation))
                    if activation is not None
                    else dbn.UNDEF_TIMESTAMP
                ),
                strike_price=int(
                    Decimal(str(item["strike_price"])) * dbn.FIXED_PRICE_SCALE
                ),
                underlying=str(item["underlying"]),
            )
        )
    metadata = dbn.Metadata(
        dataset="OPRA.PILLAR",
        start=_timestamp_ns("2024-01-02T00:00:00Z"),
        end=_timestamp_ns("2024-01-09T00:00:00Z"),
        stype_in=dbn.SType.PARENT,
        stype_out=dbn.SType.INSTRUMENT_ID,
        schema=dbn.Schema.DEFINITION,
        symbols=["TQQQ.OPT"],
    )
    return bytes(metadata) + b"".join(bytes(record) for record in records)


@pytest.fixture
def definition_sources(tmp_path: Path) -> DefinitionSources:
    definitions = [
        _definition(
            instrument_id=101,
            ts_event="2024-01-02T13:00:00Z",
            raw_symbol="TQQQ  240105C00045000",
            strike="45",
        ),
        _definition(
            instrument_id=102,
            ts_event="2024-01-02T13:00:01Z",
            raw_symbol="TQQQ  240105P00045000",
            strike="45",
            instrument_class="P",
        ),
        _definition(
            instrument_id=103,
            ts_event="2024-01-02T13:00:02Z",
            raw_symbol="TQQQ  240105C00047500",
            strike="47.5",
        ),
        _definition(
            instrument_id=104,
            ts_event="2024-01-02T13:00:03Z",
            raw_symbol="TQQQ  240105P00053000",
            strike="53",
            instrument_class="P",
        ),
        _definition(
            instrument_id=201,
            ts_event="2024-01-02T15:00:00Z",
            raw_symbol="TQQQ  240105C00045000",
            strike="45",
            action="M",
        ),
        _definition(
            instrument_id=105,
            ts_event="2024-01-02T16:15:00Z",
            raw_symbol="TQQQ  240105C00061250",
            strike="61.25",
        ),
        _definition(
            instrument_id=104,
            ts_event="2024-01-02T17:00:00Z",
            raw_symbol="TQQQ  240105P00053000",
            strike="53",
            instrument_class="P",
            action="D",
        ),
    ]
    jsonl_path = tmp_path / "definitions.jsonl"
    dbn_path = tmp_path / "definitions.dbn"
    zstd_path = tmp_path / "definitions.dbn.zst"
    jsonl_bytes = _write_fixture(jsonl_path, definitions)
    dbn_bytes = _encode_dbn(definitions)
    zstd_bytes = zstandard.ZstdCompressor().compress(dbn_bytes)
    dbn_path.write_bytes(dbn_bytes)
    zstd_path.write_bytes(zstd_bytes)
    return {
        "jsonl": (jsonl_path, jsonl_bytes),
        "dbn": (dbn_path, dbn_bytes),
        "dbn_zst": (zstd_path, zstd_bytes),
    }


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


def _providers(
    definition_sources: DefinitionSources,
) -> dict[str, DatabentoListingProvider]:
    return {
        representation: DatabentoListingProvider([path])
        for representation, (path, _) in definition_sources.items()
    }


def test_dbn_and_jsonl_definition_replay_have_structural_parity(
    definition_sources: DefinitionSources,
) -> None:
    providers = _providers(definition_sources)

    for clock in ("09:30", "10:00", "11:15", "12:01"):
        jsonl_snapshot = _query(providers["jsonl"], clock)
        dbn_snapshot = _query(providers["dbn"], clock)

        assert dbn_snapshot == jsonl_snapshot

    final_snapshot = _query(providers["dbn"], "12:01")
    assert final_snapshot.expirations[0].strikes == (
        Decimal("45"),
        Decimal("47.5"),
        Decimal("61.25"),
    )
    assert {contract.right for contract in _contracts(final_snapshot)} == {
        "CALL",
        "PUT",
    }
    assert _contracts(_query(providers["dbn"], "10:00"))[0].provider_instrument_id == 201


def test_dbn_zst_replay_matches_uncompressed_dbn(
    definition_sources: DefinitionSources,
) -> None:
    providers = _providers(definition_sources)

    for clock in ("09:30", "10:00", "11:14", "11:15", "12:01"):
        assert _query(providers["dbn_zst"], clock) == _query(providers["dbn"], clock)


def test_all_representations_share_point_in_time_activation_boundary(
    definition_sources: DefinitionSources,
) -> None:
    providers = _providers(definition_sources)

    before = {
        representation: _query(provider, "11:14")
        for representation, provider in providers.items()
    }
    at_activation = {
        representation: _query(provider, "11:15")
        for representation, provider in providers.items()
    }

    assert len(set(before.values())) == 1
    assert len(set(at_activation.values())) == 1
    assert Decimal("61.25") not in before["jsonl"].expirations[0].strikes
    assert Decimal("61.25") in at_activation["jsonl"].expirations[0].strikes


def test_source_digest_binds_each_physical_representation(
    definition_sources: DefinitionSources,
) -> None:
    providers = _providers(definition_sources)

    actual_digests = {
        representation: provider.source_digests[0][1]
        for representation, provider in providers.items()
    }
    expected_digests = {
        representation: hashlib.sha256(raw_bytes).hexdigest()
        for representation, (_, raw_bytes) in definition_sources.items()
    }

    assert actual_digests == expected_digests
    assert len(set(actual_digests.values())) == 3
    assert _query(providers["jsonl"], "11:15") == _query(providers["dbn"], "11:15")
    assert _query(providers["dbn"], "11:15") == _query(
        providers["dbn_zst"], "11:15"
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
