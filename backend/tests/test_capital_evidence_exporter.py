import hashlib
import importlib.util
import json
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.domain.enums import OptionRight
from engine.data.corpus_qualifier import (
    CorpusQualificationManifest,
    PilotWindow,
    QualificationMetrics,
    QualificationStatus,
)
from engine.data.option_enrollment import (
    CanonicalResolutionAccounting,
    deterministic_option_instrument_id,
)
from engine.data.theta_v3 import DecodedThetaSection, ThetaDecodedArtifactSerializer
from engine.validation.models import (
    CanonicalMarketBar,
    CanonicalOptionChainSnapshot,
    CanonicalOptionContractQuote,
)


ROOT = Path(__file__).resolve().parents[2]


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def qualification(dataset_hash, signal_count=2):
    accounting = CanonicalResolutionAccounting(
        discovered_contracts_count=signal_count,
        resolved_existing_contracts_count=signal_count,
        newly_enrolled_contracts_count=0,
        resolved_contracts_count=signal_count,
        rejected_contracts_count=0,
    )
    draft = CorpusQualificationManifest(
        qualification_manifest_id=uuid5(NAMESPACE_URL, "qualification"),
        qualification_manifest_sha256="0" * 64,
        qualification_policy_version="CORPUS-QUALIFICATION-v1",
        provider_code="THETA_DATA",
        pilot_window=PilotWindow(
            start_session=date(2024, 1, 2), end_session=date(2024, 3, 28),
            total_calendar_sessions=61, rth_expected_minutes=23790,
        ),
        metrics=QualificationMetrics(
            underlying_bar_completeness_pct=Decimal("100"),
            underlying_status=QualificationStatus.PASS,
            strategy_signal_count=signal_count,
            decision_point_complete_evidence_count=signal_count,
            decision_point_evidence_pct=Decimal("100"),
            decision_evidence_status=QualificationStatus.PASS,
            causal_timestamp_violations_count=0,
            causal_status=QualificationStatus.PASS,
            canonical_contract_resolution_pct=Decimal("100"),
            resolution_status=QualificationStatus.PASS,
            resolution_accounting=accounting,
            assigned_fidelity_tier="TIER_1_QUOTE_DEPTH",
            fidelity_status=QualificationStatus.PASS,
        ),
        overall_qualification_verdict=QualificationStatus.PASS,
        raw_artifacts_manifest_sha256="b" * 64,
        normalized_dataset_manifest_sha256=dataset_hash,
    )
    body = draft.model_dump(
        mode="json",
        exclude={"qualification_manifest_id", "qualification_manifest_sha256"},
    )
    digest = hashlib.sha256(canonical(body)).hexdigest()
    return draft.model_copy(update={
        "qualification_manifest_id": uuid5(
            NAMESPACE_URL, f"kairo:corpus-qualification:{digest}"
        ),
        "qualification_manifest_sha256": digest,
    })


def bar(symbol, instrument_id, completed_at, close):
    value = Decimal(close)
    return CanonicalMarketBar(
        instrument_id=instrument_id,
        symbol=symbol,
        interval_start_at=completed_at - timedelta(minutes=1),
        completed_at=completed_at,
        open=value, high=value, low=value, close=value, volume=Decimal("100"),
    )


def snapshot(symbol, underlying_id, contract_id, completed_at, bid, ask):
    return CanonicalOptionChainSnapshot(
        underlying_instrument_id=underlying_id,
        underlying_symbol=symbol,
        canonical_completed_at=completed_at,
        contracts=(CanonicalOptionContractQuote(
            contract_instrument_id=contract_id,
            underlying_instrument_id=underlying_id,
            underlying_symbol=symbol,
            canonical_contract_symbol=f"{symbol}240102P00010000",
            expiration_date=date(2024, 1, 2),
            strike_price=Decimal("10"),
            option_right=OptionRight.PUT,
            contract_multiplier=Decimal("100"),
            listing_type="STANDARD",
            bid_price=Decimal(bid), ask_price=Decimal(ask),
            bid_size=Decimal("10"), ask_size=Decimal("10"),
            volume=10, open_interest=50, liquidity_verifiable=True,
        ),),
    )


def fixture(tmp_path, *, include_exit=True, signal_count=2):
    artifact_root = tmp_path / "historical-market"
    aggregate_root = tmp_path / "aggregates"
    aggregate_root.mkdir()
    streams = []
    ordinal = 0
    for symbol in ("TQQQ", "SQQQ"):
        underlying_id = uuid5(NAMESPACE_URL, f"underlying:{symbol}")
        contract_id = deterministic_option_instrument_id(
            symbol, date(2024, 1, 2), Decimal("10"), OptionRight.PUT
        )
        start = datetime(2024, 1, 2, 14, 31, tzinfo=UTC)
        bars = [bar(symbol, underlying_id, start + timedelta(minutes=i), "10") for i in range(9)]
        bars.append(bar(symbol, underlying_id, start + timedelta(minutes=9), "9"))
        bars.append(bar(symbol, underlying_id, start + timedelta(minutes=10), "9"))
        bars.append(bar(
            symbol, underlying_id, datetime(2024, 3, 28, 14, 31, tzinfo=UTC), "10"
        ))
        option_rows = [snapshot(
            symbol, underlying_id, contract_id, start + timedelta(minutes=9), "0.19", "0.20"
        )]
        if include_exit:
            option_rows.append(snapshot(
                symbol, underlying_id, contract_id, start + timedelta(minutes=10), "0.25", "0.26"
            ))
        quote_rows = [
            {
                "symbol": symbol,
                "strike": Decimal("10"),
                "right": "PUT",
                "timestamp": item.canonical_completed_at,
                "bid": item.contracts[0].bid_price,
                "ask": item.contracts[0].ask_price,
                "bid_size": 10,
                "ask_size": 10,
            }
            for item in option_rows
        ]
        aggregate = ThetaDecodedArtifactSerializer().serialize(
            [DecodedThetaSection(
                endpoint="option_history_quote",
                parameters={
                    "symbol": symbol,
                    "expiration": date(2024, 1, 2),
                    "date": date(2024, 1, 2),
                    "interval": "1m",
                    "start_time": datetime.min.time().replace(hour=9, minute=30),
                    "end_time": datetime.min.time().replace(hour=9, minute=41),
                },
                dataframe=quote_rows,
            )],
            acquisition_request={"request_kind": "fixture", "symbol": symbol},
        )
        aggregate_path = aggregate_root / f"{symbol}-options.bin"
        aggregate_path.write_bytes(aggregate)
        aggregate_hash = hashlib.sha256(aggregate).hexdigest()
        for role, rows in (
            ("UNDERLYING_SIGNAL_BARS", bars),
            ("OPTION_CHAIN_QUOTES", option_rows),
        ):
            content = canonical([item.model_dump(mode="json") for item in rows])
            digest = hashlib.sha256(content).hexdigest()
            target = artifact_root / digest[:2] / digest[2:4] / f"{digest}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            streams.append({
                "instrument_id": str(underlying_id), "symbol": symbol,
                "stream_role": role, "stream_ordinal": ordinal,
                "raw_content_sha256": (
                    aggregate_hash
                    if role == "OPTION_CHAIN_QUOTES"
                    else hashlib.sha256(f"raw:{ordinal}".encode()).hexdigest()
                ),
                "normalized_content_sha256": digest, "bar_count": len(rows),
                "first_bar_start_at": (
                    rows[0].interval_start_at if role == "UNDERLYING_SIGNAL_BARS"
                    else rows[0].canonical_completed_at
                ).isoformat(),
                "last_bar_completed_at": (
                    rows[-1].completed_at if role == "UNDERLYING_SIGNAL_BARS"
                    else rows[-1].canonical_completed_at
                ).isoformat(),
            })
            ordinal += 1
    dataset = {
        "dataset_name": "THETA-PILOT-2024-01-02-2024-03-28",
        "provider_name": "THETA_DATA",
        "replay_mode": "RESEARCH_REPLAY_MODE",
        "exact_prototype_replay": False,
        "calendar_version": "CAL-US-EQUITIES-2026-v1",
        "normalization_policy_version": "NORM-PILOT-CORPUS-v1",
        "streams": streams,
    }
    dataset_content = canonical(dataset)
    dataset_hash = hashlib.sha256(dataset_content).hexdigest()
    dataset_path = tmp_path / "dataset-manifest.json"
    dataset_path.write_bytes(dataset_content)
    manifest_content = qualification(dataset_hash, signal_count).canonical_bytes()
    manifest_path = tmp_path / "qualification.json"
    manifest_path.write_bytes(manifest_content)
    return {
        "qualification_manifest_uri": str(manifest_path),
        "qualification_manifest_sha256": hashlib.sha256(manifest_content).hexdigest(),
        "dataset_manifest_uri": str(dataset_path),
        "artifact_root": str(artifact_root),
        "option_aggregate_root": str(aggregate_root),
    }


def replay_fixture(exporter, *, entry_at, exit_bid, exit_ask, exit_close="9"):
    symbol = "TQQQ"
    underlying_id = uuid5(NAMESPACE_URL, "underlying:replay")
    contract_id = deterministic_option_instrument_id(
        symbol, entry_at.date(), Decimal("10"), OptionRight.PUT
    )
    bars = [
        bar(
            symbol,
            underlying_id,
            entry_at - timedelta(minutes=9 - index),
            "10",
        )
        for index in range(9)
    ]
    entry_bar = bar(symbol, underlying_id, entry_at, "9")
    exit_bar = bar(
        symbol, underlying_id, entry_at + timedelta(minutes=1), exit_close
    )
    bars.extend((entry_bar, exit_bar))
    entry_snapshot = snapshot(
        symbol, underlying_id, contract_id, entry_at, "0.19", "0.20"
    )
    plan = exporter.EntryPlan(bar=entry_bar, quote=entry_snapshot.contracts[0])
    quotes = (
        exporter.IntraTradeQuote(
            timestamp=entry_at, bid=Decimal("0.19"), ask=Decimal("0.20")
        ),
        exporter.IntraTradeQuote(
            timestamp=entry_at + timedelta(minutes=1),
            bid=Decimal(exit_bid),
            ask=Decimal(exit_ask),
        ),
    )
    return plan, bars, quotes


def test_export_is_byte_deterministic_and_self_sealed(tmp_path):
    exporter = load_module(
        "kairo_capital_exporter", "scripts/research/export_q1_capital_evidence.py"
    )
    runner = load_module(
        "kairo_capital_runner_test", "scripts/research/run_q1_capital_matrix.py"
    )
    inputs = fixture(tmp_path)
    first = exporter.export_evidence(**inputs)
    second = exporter.export_evidence(**inputs)
    assert exporter.evidence_bytes(first) == exporter.evidence_bytes(second)
    assert len(first.signals) == 2
    assert {item.symbol for item in first.signals} == {"TQQQ", "SQQQ"}
    assert len(first.artifacts) == 6
    first.verify_self_seal()
    output = tmp_path / "evidence.json"
    content = exporter.evidence_bytes(first)
    output.write_bytes(content)
    loaded_manifest, loaded = runner.load_certified_evidence(
        manifest_uri=inputs["qualification_manifest_uri"],
        manifest_sha256=inputs["qualification_manifest_sha256"],
        evidence_uri=str(output),
        evidence_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert loaded.normalized_dataset_manifest_sha256 == (
        loaded_manifest.normalized_dataset_manifest_sha256
    )
    assert loaded.qualification_manifest_sha256 == (
        loaded_manifest.qualification_manifest_sha256
    )


def test_evidence_self_seal_fails_closed_after_payload_tampering(tmp_path):
    exporter = load_module(
        "kairo_capital_exporter_seal", "scripts/research/export_q1_capital_evidence.py"
    )
    evidence = exporter.export_evidence(**fixture(tmp_path))
    changed_signal = evidence.signals[0].model_copy(update={"entry_bid": Decimal("0.18")})
    tampered = evidence.model_copy(update={
        "signals": (changed_signal, *evidence.signals[1:])
    })
    with pytest.raises(ValueError, match="self-seal"):
        tampered.verify_self_seal()


def test_qualification_internal_identity_is_verified(tmp_path):
    exporter = load_module(
        "kairo_capital_exporter_qualification_identity",
        "scripts/research/export_q1_capital_evidence.py",
    )
    inputs = fixture(tmp_path)
    path = Path(inputs["qualification_manifest_uri"])
    manifest = CorpusQualificationManifest.model_validate_json(path.read_bytes())
    corrupted = manifest.model_copy(update={"qualification_manifest_sha256": "f" * 64})
    content = corrupted.canonical_bytes()
    path.write_bytes(content)
    inputs["qualification_manifest_sha256"] = hashlib.sha256(content).hexdigest()
    with pytest.raises(ValueError, match="internal SHA-256"):
        exporter.export_evidence(**inputs)


def test_export_contains_only_minimal_execution_fields(tmp_path):
    exporter = load_module(
        "kairo_capital_exporter_minimal", "scripts/research/export_q1_capital_evidence.py"
    )
    evidence = exporter.export_evidence(**fixture(tmp_path))
    assert set(evidence.signals[0].model_dump()) == {
        "signal_id", "contract_id", "underlying", "right",
        "session", "entry_timestamp", "exit_timestamp",
        "entry_bid", "entry_ask", "exit_bid", "exit_ask",
        "contract_multiplier", "exit_reason", "intra_trade_path",
    }


def test_option_artifacts_are_streamed_instead_of_eagerly_materialized(
    tmp_path, monkeypatch
):
    exporter = load_module(
        "kairo_capital_exporter_streaming",
        "scripts/research/export_q1_capital_evidence.py",
    )
    eager_payloads = []
    original = exporter._load_canonical_array

    def observed_loader(uri, digest):
        result = original(uri, digest)
        eager_payloads.append(result[1])
        return result

    monkeypatch.setattr(exporter, "_load_canonical_array", observed_loader)
    exporter.export_evidence(**fixture(tmp_path))
    assert len(eager_payloads) == 2
    assert all("symbol" in row for payload in eager_payloads for row in payload)


@pytest.mark.parametrize(
    ("exit_bid", "exit_ask", "exit_close", "expected_reason"),
    (
        ("0.22", "0.23", "9", "TAKE_PROFIT"),
        ("0.19", "0.20", "9", "STOP_LOSS"),
        ("0.20", "0.21", "11", "TREND_REVERSAL"),
    ),
)
def test_causal_replay_uses_first_frozen_exit_condition(
    exit_bid, exit_ask, exit_close, expected_reason
):
    exporter = load_module(
        f"kairo_capital_exporter_exit_{expected_reason.lower()}",
        "scripts/research/export_q1_capital_evidence.py",
    )
    plan, bars, quotes = replay_fixture(
        exporter,
        entry_at=datetime(2024, 1, 2, 14, 40, tzinfo=UTC),
        exit_bid=exit_bid,
        exit_ask=exit_ask,
        exit_close=exit_close,
    )
    result = exporter._replay_entry(
        plan, bars, quotes, dataset_sha256="d" * 64
    )
    assert result.exit_reason == expected_reason
    assert result.intra_trade_path == quotes


def test_1545_flatten_requires_exact_1545_quote_and_never_aliases_1544():
    exporter = load_module(
        "kairo_capital_exporter_flatten",
        "scripts/research/export_q1_capital_evidence.py",
    )
    entry_at = datetime(2024, 1, 2, 20, 44, tzinfo=UTC)
    plan, bars, quotes = replay_fixture(
        exporter,
        entry_at=entry_at,
        exit_bid="0.20",
        exit_ask="0.21",
    )
    with pytest.raises(ValueError, match="20:45:00"):
        exporter._replay_entry(
            plan, bars, quotes[:1], dataset_sha256="d" * 64
        )
    later = exporter.IntraTradeQuote(
        timestamp=entry_at + timedelta(minutes=2),
        bid=Decimal("0.30"),
        ask=Decimal("0.31"),
    )
    result = exporter._replay_entry(
        plan, bars, (*quotes, later), dataset_sha256="d" * 64
    )
    assert result.exit_reason == "FORCED_FLATTEN"
    assert result.exit_at == entry_at + timedelta(minutes=1)
    assert result.intra_trade_path == quotes
    assert exporter.QUOTE_TIMESTAMP_CONVENTION == (
        "THETA_LAST_QUOTE_AT_INTERVAL_TIMESTAMP"
    )


def test_corrupt_normalized_artifact_fails_closed(tmp_path):
    exporter = load_module(
        "kairo_capital_exporter_corrupt", "scripts/research/export_q1_capital_evidence.py"
    )
    inputs = fixture(tmp_path)
    artifact = next(Path(inputs["artifact_root"]).rglob("*.json"))
    artifact.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="SHA-256"):
        exporter.export_evidence(**inputs)


def test_corrupt_binary_aggregate_fails_closed_before_replay(tmp_path):
    exporter = load_module(
        "kairo_capital_exporter_corrupt_binary",
        "scripts/research/export_q1_capital_evidence.py",
    )
    inputs = fixture(tmp_path)
    aggregate = Path(inputs["option_aggregate_root"]) / "TQQQ-options.bin"
    aggregate.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="SHA-256"):
        exporter.export_evidence(**inputs)


def test_dataset_manifest_hash_must_match_qualification(tmp_path):
    exporter = load_module(
        "kairo_capital_exporter_dataset", "scripts/research/export_q1_capital_evidence.py"
    )
    inputs = fixture(tmp_path)
    Path(inputs["dataset_manifest_uri"]).write_bytes(b"{}")
    with pytest.raises(ValueError, match="SHA-256"):
        exporter.export_evidence(**inputs)


def test_database_authority_rows_reconstruct_exact_dataset_and_artifact_refs(tmp_path):
    exporter = load_module(
        "kairo_capital_exporter_db_rows",
        "scripts/research/export_q1_capital_evidence.py",
    )
    inputs = fixture(tmp_path)
    body = json.loads(Path(inputs["dataset_manifest_uri"]).read_bytes())
    dataset_hash = hashlib.sha256(canonical(body)).hexdigest()
    dataset = SimpleNamespace(
        dataset_name=body["dataset_name"],
        provider_name=body["provider_name"],
        calendar_version=body["calendar_version"],
        normalization_policy_version=body["normalization_policy_version"],
        dataset_manifest_sha256=dataset_hash,
    )
    entries = []
    artifacts = {}
    for stream in body["streams"]:
        raw_id = uuid5(NAMESPACE_URL, f"raw:{stream['stream_ordinal']}")
        normalized_id = uuid5(NAMESPACE_URL, f"normalized:{stream['stream_ordinal']}")
        digest = stream["normalized_content_sha256"]
        path = (
            Path(inputs["artifact_root"])
            / digest[:2]
            / digest[2:4]
            / f"{digest}.json"
        )
        entry_fields = dict(stream)
        entry_fields["first_bar_start_at"] = datetime.fromisoformat(
            stream["first_bar_start_at"]
        )
        entry_fields["last_bar_completed_at"] = datetime.fromisoformat(
            stream["last_bar_completed_at"]
        )
        entries.append(SimpleNamespace(
            **entry_fields,
            raw_artifact_id=raw_id,
            normalized_artifact_id=normalized_id,
        ))
        artifacts[raw_id] = SimpleNamespace(
            artifact_role="RAW_PROVIDER_PAYLOAD",
            content_sha256=stream["raw_content_sha256"],
            storage_uri=str(tmp_path / f"raw-{stream['stream_ordinal']}.bin"),
            byte_size=1,
        )
        artifacts[normalized_id] = SimpleNamespace(
            artifact_role="NORMALIZED_RESEARCH_STREAM",
            content_sha256=digest,
            mime_type="application/json",
            storage_uri=str(path),
            byte_size=path.stat().st_size,
        )
    reconstructed, references = exporter._dataset_from_authority_rows(
        dataset, tuple(entries), artifacts
    )
    assert canonical(reconstructed) == canonical(body)
    assert tuple(references) == (0, 1, 2, 3)
    assert all(
        reference.normalized.uri.endswith(".json")
        for reference in references.values()
    )
    entries[0].bar_count += 1
    with pytest.raises(ValueError, match="reconstruct"):
        exporter._dataset_from_authority_rows(dataset, tuple(entries), artifacts)


def test_incomplete_exit_evidence_fails_without_synthetic_outcome(tmp_path):
    exporter = load_module(
        "kairo_capital_exporter_exit", "scripts/research/export_q1_capital_evidence.py"
    )
    with pytest.raises(ValueError, match="quote path is incomplete"):
        exporter.export_evidence(**fixture(tmp_path, include_exit=False))


def test_signal_population_must_match_qualification(tmp_path):
    exporter = load_module(
        "kairo_capital_exporter_population", "scripts/research/export_q1_capital_evidence.py"
    )
    with pytest.raises(ValueError, match="signal population"):
        exporter.export_evidence(**fixture(tmp_path, signal_count=3))


def test_staging_artifact_root_is_rejected_before_read(tmp_path):
    exporter = load_module(
        "kairo_capital_exporter_staging", "scripts/research/export_q1_capital_evidence.py"
    )
    inputs = fixture(tmp_path)
    inputs["artifact_root"] = str(tmp_path / ".attempt-4-staging-v1")
    with pytest.raises(ValueError, match="staging"):
        exporter.export_evidence(**inputs)


def test_cli_writes_canonical_bundle_and_reports_hash(tmp_path, capsys):
    exporter = load_module(
        "kairo_capital_exporter_cli", "scripts/research/export_q1_capital_evidence.py"
    )
    inputs = fixture(tmp_path)
    output = tmp_path / "sealed-evidence.json"
    argv = [
        "--qualification-manifest-uri", inputs["qualification_manifest_uri"],
        "--qualification-manifest-sha256", inputs["qualification_manifest_sha256"],
        "--dataset-manifest-uri", inputs["dataset_manifest_uri"],
        "--artifact-root", inputs["artifact_root"],
        "--option-aggregate-root", inputs["option_aggregate_root"],
        "--output", str(output),
    ]
    assert exporter.main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["content_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert report["signals"] == 2
    assert output.read_bytes() == canonical(json.loads(output.read_bytes()))
