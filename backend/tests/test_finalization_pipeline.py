import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from engine.data.corpus_qualifier import QualificationStatus
from engine.data.corpus_qualifier_v21 import CorpusQualificationV21Manifest
from engine.data.streaming_pilot import (
    OptionDiscoverySpool,
    SessionLiquidityIndex,
    scan_decoded_aggregate,
)
from engine.data.theta_v3 import DecodedThetaSection, ThetaDecodedArtifactSerializer
from kairo.pipeline.finalization_stages import (
    Progress,
    SOURCE_OBJECTS,
    _authority_exists,
    run_stage_1,
)
from kairo.pipeline.finalize import receipt_chain
from kairo.pipeline.finalization_state import (
    AUTHORITY_STAGE_VERSION,
    DATASET,
    ObjectIdentity,
    RECEIPT_VERSION,
    Receipt,
    load_receipt,
    receipt_path,
    seal_receipt,
)


class MemoryStore:
    def __init__(self):
        self.values = {}
        self.identities = {}
        self.generation = 0

    def add_source(self, name, content):
        self.generation += 1
        self.values[name] = content
        self.identities[name] = ObjectIdentity(
            uri=f"gs://kairo-market-artifacts-507516/{name}",
            generation=str(self.generation),
            metageneration="1",
            byte_count=len(content),
            sha256=None,
        )

    def stat(self, object_name):
        return self.identities.get(object_name)

    def download(self, object_name, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.values[object_name])
        return self.identities[object_name]

    def upload_immutable(self, object_name, source, *, content_type, sha256, byte_count):
        content = source.read_bytes()
        assert hashlib.sha256(content).hexdigest() == sha256
        assert len(content) == byte_count
        existing = self.identities.get(object_name)
        if existing:
            if self.values[object_name] != content:
                raise ValueError("conflict")
            return existing
        self.generation += 1
        identity = ObjectIdentity(
            uri=f"gs://kairo-market-artifacts-507516/{object_name}",
            generation=str(self.generation),
            metageneration="1",
            byte_count=byte_count,
            sha256=sha256,
        )
        self.values[object_name] = content
        self.identities[object_name] = identity
        return identity

    def read_bytes(self, object_name):
        return self.values[object_name]

    def seal_bytes(self, object_name, content, *, content_type):
        existing = self.identities.get(object_name)
        if existing:
            if self.values[object_name] != content:
                raise ValueError("conflict")
            return existing
        self.generation += 1
        identity = ObjectIdentity(
            uri=f"gs://kairo-market-artifacts-507516/{object_name}",
            generation=str(self.generation),
            metageneration="1",
            byte_count=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        self.values[object_name] = content
        self.identities[object_name] = identity
        return identity


class ScalarSequence:
    def __init__(self, *values):
        self.values = iter(values)

    def scalar(self, _statement):
        return next(self.values)


def aggregate(symbol="TQQQ"):
    observed = datetime(2024, 1, 2, 15, 31, tzinfo=timezone.utc)
    return ThetaDecodedArtifactSerializer().serialize(
        (
            DecodedThetaSection(
                endpoint="option_history_quote",
                parameters={"symbol": symbol, "expiration": date(2024, 1, 5)},
                dataframe=[{
                    "strike": Decimal("50"),
                    "right": "CALL",
                    "timestamp": observed,
                    "bid": Decimal("1"),
                    "ask": Decimal("1.1"),
                    "bid_size": 2,
                    "ask_size": 3,
                }],
            ),
            DecodedThetaSection(
                endpoint="option_history_open_interest",
                parameters={"date": date(2024, 1, 2), "expiration": date(2024, 1, 5)},
                dataframe=[{
                    "strike": Decimal("50"),
                    "right": "CALL",
                    "timestamp": observed,
                    "open_interest": 9,
                }],
            ),
        ),
        acquisition_request={"symbol": symbol},
    )


def test_single_pass_scanner_matches_existing_index_semantics(tmp_path):
    content = aggregate()
    source = tmp_path / "aggregate.bin"
    source.write_bytes(content)
    baseline_discovery = tmp_path / "baseline-discovery.sqlite3"
    baseline_liquidity = tmp_path / "baseline-liquidity.sqlite3"
    fused_discovery = tmp_path / "fused-discovery.sqlite3"
    fused_liquidity = tmp_path / "fused-liquidity.sqlite3"

    from engine.data.streaming_pilot import iter_decoded_sections, staged_artifact

    with OptionDiscoverySpool(baseline_discovery, "TQQQ") as discovery, SessionLiquidityIndex(
        baseline_liquidity
    ) as liquidity:
        for section in iter_decoded_sections(staged_artifact(source, "application/octet-stream")):
            discovery.ingest_sections((section,), commit=False)
            liquidity.ingest_sections((section,), commit=False)
        discovery.commit()
        liquidity.commit()

    with OptionDiscoverySpool(fused_discovery, "TQQQ") as discovery, SessionLiquidityIndex(
        fused_liquidity
    ) as liquidity:
        result = scan_decoded_aggregate(
            source,
            section_sinks=(
                lambda section: discovery.ingest_sections((section,), commit=False),
                lambda section: liquidity.ingest_sections((section,), commit=False),
            ),
        )
        discovery.commit()
        liquidity.commit()
        assert discovery.row_counts() == {
            "valid_discoveries": 1,
            "rejected_discoveries": 0,
            "accepted": 0,
        }
        assert liquidity.row_count() == 1

    assert result.artifact.content_sha256 == hashlib.sha256(content).hexdigest()
    assert result.artifact.byte_size == len(content)
    for baseline, fused in (
        (baseline_discovery, fused_discovery),
        (baseline_liquidity, fused_liquidity),
    ):
        # SQLite page layout is deterministic for identical ordered statements.
        assert baseline.read_bytes() == fused.read_bytes()


def test_stage_1_seals_generation_bound_indexes_and_receipt(tmp_path):
    store = MemoryStore()
    paths = {}
    for symbol in ("TQQQ", "SQQQ"):
        content = aggregate(symbol)
        store.add_source(SOURCE_OBJECTS[symbol], content)
        path = tmp_path / f"{symbol}.bin"
        path.write_bytes(content)
        paths[symbol] = path
    receipt = run_stage_1(store, tmp_path, Progress(lambda _value: None), source_paths=paths)
    restored = load_receipt(store, 1)
    assert restored == receipt
    assert receipt.facts["aggregates"]["TQQQ"]["source_sha256"] == hashlib.sha256(
        aggregate("TQQQ")
    ).hexdigest()
    assert len(receipt.outputs) == 4
    assert all(item["identity"]["generation"] for item in receipt.inputs)
    assert all(item["identity"]["sha256"] for item in receipt.outputs)


def test_stage_1_verification_failure_publishes_nothing(tmp_path):
    store = MemoryStore()
    paths = {}
    for symbol in ("TQQQ", "SQQQ"):
        content = aggregate(symbol)
        store.add_source(SOURCE_OBJECTS[symbol], content)
        path = tmp_path / f"{symbol}.bin"
        path.write_bytes(content if symbol == "TQQQ" else content[:-1])
        paths[symbol] = path
    with pytest.raises(ValueError):
        run_stage_1(store, tmp_path, Progress(lambda _value: None), source_paths=paths)
    assert store.stat(receipt_path(1)) is None
    assert not any("/indexes/" in name for name in store.values)


def test_receipt_parser_rejects_noncanonical_and_stale_contract():
    receipt = Receipt(
        receipt_version="KAIRO-Q1-FINALIZATION-RECEIPT-v1",
        dataset="q1-2024",
        stage=1,
        stage_version="v1",
        predecessor_sha256=None,
        inputs=(),
        outputs=(),
        facts={},
    )
    assert Receipt.parse(receipt.canonical_bytes()) == receipt
    with pytest.raises(ValueError, match="not canonical"):
        Receipt.parse(receipt.canonical_bytes() + b"\n")
    with pytest.raises(ValueError, match="contract mismatch"):
        Receipt.parse(replace(receipt, dataset="other").canonical_bytes())


def test_authority_requires_dataset_and_both_qualification_lineages():
    plan = {
        "expected_dataset_manifest_sha256": "a" * 64,
        "expected_qualification": {"qualification_policy_version": "v1"},
    }
    v1_bytes = b'{"qualification_policy_version":"v1"}'
    v21_bytes = b'{"qualification_policy_version":"v2.1"}'
    v1 = SimpleNamespace(
        artifact_role="NORMALIZED_RESEARCH_STREAM",
        byte_size=len(v1_bytes),
        mime_type="application/vnd.kairo.corpus-qualification+json",
    )
    v21 = SimpleNamespace(
        artifact_role="NORMALIZED_RESEARCH_STREAM",
        byte_size=len(v21_bytes),
        mime_type="application/vnd.kairo.corpus-qualification+json",
    )

    assert _authority_exists(ScalarSequence(None, None, None), plan, v21_bytes) is False
    assert _authority_exists(ScalarSequence(object(), v1, v21), plan, v21_bytes) is True
    with pytest.raises(ValueError, match="partial canonical authority"):
        _authority_exists(ScalarSequence(object(), v1, None), plan, v21_bytes)


def test_stage_3_receipt_serializes_decimal_diagnostic_through_canonical_path():
    def contains_decimal(value):
        if isinstance(value, dict):
            return any(contains_decimal(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(contains_decimal(item) for item in value)
        return isinstance(value, Decimal)

    diagnostic = {
        "decision_count": 9454,
        "eligible_candidate_decision_count": 9453,
        "candidate_availability_percentage": Decimal("99.99"),
        "scoring_effect": "NONE",
        "live_capital_authorization": False,
    }
    expected_v21 = CorpusQualificationV21Manifest.model_construct(
        qualification_manifest_id=UUID("2d74c24a-55bd-5dba-a646-899babc57fd1"),
        qualification_manifest_sha256="9" * 64,
        qualification_policy_version="CORPUS-QUALIFICATION-v2.1",
        provider_code="THETA_DATA",
        stage_2_predecessor={"receipt_sha256": "2" * 64},
        v1_lineage={"qualification_policy_version": "CORPUS-QUALIFICATION-v1"},
        pilot_window={"total_calendar_sessions": 61},
        scored_acquisition_qualification={
            "acquisition_envelope": {"combined": {"completeness_percentage": Decimal("41.58")}}
        },
        strategy_001_diagnostic=diagnostic,
        overall_qualification_verdict=QualificationStatus.FAIL,
        raw_artifacts_manifest_sha256="3" * 64,
        normalized_dataset_manifest_sha256="4" * 64,
    )
    expected_v21_bytes = expected_v21.canonical_bytes()
    receipt = Receipt(
        receipt_version=RECEIPT_VERSION,
        dataset=DATASET,
        stage=3,
        stage_version=AUTHORITY_STAGE_VERSION,
        predecessor_sha256="5" * 64,
        inputs=(),
        outputs=(),
        facts={
            "dataset_manifest_sha256": "4" * 64,
            "qualification": expected_v21.model_dump(mode="json"),
            "qualification_v1_lineage": expected_v21.v1_lineage,
            "qualification_manifest_bytes_sha256": hashlib.sha256(
                expected_v21_bytes
            ).hexdigest(),
            "strategy_001_diagnostic": expected_v21.model_dump(mode="json")[
                "strategy_001_diagnostic"
            ],
        },
    )
    store = MemoryStore()

    seal_receipt(store, receipt)

    content = store.read_bytes(receipt_path(3))
    restored = Receipt.parse(content)
    payload = json.loads(content)
    serialized_diagnostic = payload["facts"]["strategy_001_diagnostic"]
    assert restored == receipt
    assert not contains_decimal(payload)
    assert Decimal(serialized_diagnostic["candidate_availability_percentage"]) == Decimal(
        "99.99"
    )
    assert payload["facts"]["qualification"]["overall_qualification_verdict"] == "FAIL"
    assert serialized_diagnostic["scoring_effect"] == "NONE"
    assert serialized_diagnostic["live_capital_authorization"] is False


def test_restart_chain_fails_closed_on_gap_and_source_generation_drift(tmp_path):
    gap_store = MemoryStore()
    stage_2 = Receipt(
        receipt_version="KAIRO-Q1-FINALIZATION-RECEIPT-v1",
        dataset="q1-2024",
        stage=2,
        stage_version="v1",
        predecessor_sha256="a" * 64,
        inputs=(),
        outputs=(),
        facts={},
    )
    gap_store.seal_bytes(receipt_path(2), stage_2.canonical_bytes(), content_type="application/json")
    with pytest.raises(ValueError, match="stage gap"):
        receipt_chain(gap_store)

    store = MemoryStore()
    paths = {}
    for symbol in ("TQQQ", "SQQQ"):
        content = aggregate(symbol)
        store.add_source(SOURCE_OBJECTS[symbol], content)
        paths[symbol] = tmp_path / f"{symbol}.bin"
        paths[symbol].write_bytes(content)
    run_stage_1(store, tmp_path, Progress(lambda _value: None), source_paths=paths)
    source = store.identities[SOURCE_OBJECTS["TQQQ"]]
    store.identities[SOURCE_OBJECTS["TQQQ"]] = replace(
        source, generation=str(int(source.generation) + 100)
    )
    with pytest.raises(ValueError, match="identity contradiction"):
        receipt_chain(store)
