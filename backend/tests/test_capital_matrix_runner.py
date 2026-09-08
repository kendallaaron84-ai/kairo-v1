import hashlib
import importlib.util
import json
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from engine.data.corpus_qualifier import (
    CorpusQualificationManifest,
    PilotWindow,
    QualificationMetrics,
    QualificationStatus,
)
from engine.data.option_enrollment import CanonicalResolutionAccounting


ROOT = Path(__file__).resolve().parents[2]
UTC = timezone.utc


def load_runner():
    path = ROOT / "scripts" / "research" / "run_q1_capital_matrix.py"
    spec = importlib.util.spec_from_file_location("kairo_q1_capital_matrix", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def signal(
    runner,
    index: int,
    *,
    entry_bid: str = "0.19",
    entry_ask: str = "0.21",
    exit_bid: str = "0.29",
    exit_ask: str = "0.31",
    session: date = date(2024, 1, 2),
):
    at = datetime.combine(session, datetime.min.time(), UTC) + timedelta(hours=15, minutes=index)
    return runner.EmpiricalSignal(
        signal_id=f"signal-{index:03d}",
        symbol="TQQQ" if index % 2 == 0 else "SQQQ",
        session=session,
        signal_at=at,
        exit_at=at + timedelta(minutes=5),
        entry_bid=Decimal(entry_bid),
        entry_ask=Decimal(entry_ask),
        exit_bid=Decimal(exit_bid),
        exit_ask=Decimal(exit_ask),
        contract_multiplier=Decimal("100"),
        exit_reason="TAKE_PROFIT" if Decimal(exit_bid) > Decimal(entry_ask) else "STOP_LOSS",
    )


def qualification(signal_count: int, verdict=QualificationStatus.PASS):
    accounting = CanonicalResolutionAccounting(
        discovered_contracts_count=signal_count,
        resolved_existing_contracts_count=signal_count,
        newly_enrolled_contracts_count=0,
        resolved_contracts_count=signal_count,
        rejected_contracts_count=0,
    )
    return CorpusQualificationManifest(
        qualification_manifest_id=uuid4(),
        qualification_manifest_sha256="a" * 64,
        qualification_policy_version="CORPUS-QUALIFICATION-v1",
        provider_code="THETA_DATA",
        pilot_window=PilotWindow(
            start_session=date(2024, 1, 2),
            end_session=date(2024, 3, 28),
            total_calendar_sessions=61,
            rth_expected_minutes=23790,
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
        overall_qualification_verdict=verdict,
        raw_artifacts_manifest_sha256="b" * 64,
        normalized_dataset_manifest_sha256="c" * 64,
    )


def sealed_fixture(tmp_path, runner, signals, *, verdict=QualificationStatus.PASS):
    artifact_path = tmp_path / "canonical-normalized.json"
    artifact_content = b'{"canonical":"fixture"}'
    artifact_path.write_bytes(artifact_content)
    manifest = qualification(len(signals), verdict)
    manifest_content = manifest.canonical_bytes()
    manifest_path = tmp_path / "qualification.json"
    manifest_path.write_bytes(manifest_content)
    manifest_hash = hashlib.sha256(manifest_content).hexdigest()
    evidence = runner.Q1CapitalMatrixEvidence(
        schema_version="KAIRO-Q1-CAPITAL-EVIDENCE-v1",
        qualification_manifest_sha256=manifest_hash,
        normalized_dataset_manifest_sha256="c" * 64,
        strategy_id="EMA-CROSS-001",
        strategy_version="1.0.0",
        start_session=date(2024, 1, 2),
        end_session=date(2024, 3, 28),
        artifacts=(runner.ArtifactReference(
            uri=str(artifact_path),
            content_sha256=hashlib.sha256(artifact_content).hexdigest(),
            byte_size=len(artifact_content),
        ),),
        signals=signals,
    )
    evidence_content = json.dumps(
        evidence.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_bytes(evidence_content)
    return {
        "manifest_uri": str(manifest_path),
        "manifest_sha256": manifest_hash,
        "evidence_uri": str(evidence_path),
        "evidence_sha256": hashlib.sha256(evidence_content).hexdigest(),
        "artifact_path": artifact_path,
    }


def policy(runner, commission="0", spread="100", slippage="0"):
    return runner.FrictionPolicy(
        commission_per_contract_side=Decimal(commission),
        spread_capture_pct=Decimal(spread),
        slippage_bps=Decimal(slippage),
    )


def test_capital_parameterization_and_affordability_drop_logic():
    runner = load_runner()
    signals = (
        signal(runner, 0, entry_bid="0.19", entry_ask="0.20"),
        signal(runner, 1, entry_bid="0.49", entry_ask="0.50"),
        signal(runner, 2, entry_bid="0.99", entry_ask="1.00"),
    )
    evidence = runner.Q1CapitalMatrixEvidence(
        schema_version="KAIRO-Q1-CAPITAL-EVIDENCE-v1",
        qualification_manifest_sha256="a" * 64,
        normalized_dataset_manifest_sha256="c" * 64,
        strategy_id="EMA-CROSS-001",
        strategy_version="1.0.0",
        start_session=date(2024, 1, 2),
        end_session=date(2024, 3, 28),
        artifacts=(runner.ArtifactReference(
            uri="canonical.json", content_sha256="d" * 64, byte_size=1
        ),),
        signals=signals,
    )
    summary = runner.build_summary(
        evidence,
        manifest_sha256="a" * 64,
        evidence_sha256="e" * 64,
        capitals=runner.DEFAULT_CAPITALS,
        policy=policy(runner),
    )
    assert [item.affordability_rejections for item in summary.tiers] == [3, 2, 1, 0]
    assert [item.trades_entered for item in summary.tiers] == [0, 1, 2, 3]
    assert summary.affordability.minimum_capital_for_participation == Decimal("120.00")
    assert summary.affordability.capital_for_50_pct == Decimal("300.00")
    assert summary.affordability.capital_for_80_pct == Decimal("600.00")
    assert summary.affordability.capital_for_95_pct == Decimal("600.00")


def test_explicit_commission_spread_and_slippage_accounting():
    runner = load_runner()
    result = runner.run_tier(
        Decimal("126"),
        (signal(runner, 0),),
        policy(runner, commission="0.65", spread="100", slippage="0"),
    )
    assert result.trades_entered == 1
    assert result.gross_profit == Decimal("10.00")
    assert result.commissions_paid == Decimal("1.30")
    assert result.modeled_spread_slippage == Decimal("2.00")
    assert result.net_profit == Decimal("6.70")
    assert result.ending_capital == Decimal("132.70")
    assert result.time_in_market_pct == Decimal("0.02")
    assert result.avg_capital_deployed == Decimal("21.00")
    assert result.peak_capital_deployed == Decimal("21.00")
    assert result.trades_per_session == Decimal("0.0164")
    assert result.no_trade_sessions == 60


def test_rejected_signal_records_one_contract_shadow_economics():
    runner = load_runner()
    result = runner.run_tier(
        Decimal("100"),
        (signal(runner, 0),),
        policy(runner, commission="0.65", spread="100", slippage="0"),
    )
    assert result.affordability_rejections == 1
    assert result.trades_entered == 0
    assert result.shadow_rejected_signals == 1
    assert result.shadow_gross_profit == Decimal("10.00")
    assert result.shadow_gross_loss == Decimal("0.00")
    assert result.shadow_commissions_paid == Decimal("1.30")
    assert result.shadow_modeled_spread_slippage == Decimal("2.00")
    assert result.shadow_net_profit == Decimal("6.70")
    assert result.shadow_rejected_outcomes[0].model_dump() == {
        "signal_id": "signal-000",
        "required_cell_capital": Decimal("126.00"),
        "gross_outcome": Decimal("10.00"),
        "commissions": Decimal("1.30"),
        "modeled_spread_slippage": Decimal("2.00"),
        "net_outcome": Decimal("6.70"),
    }


def test_economic_viability_is_separate_from_participation_threshold():
    runner = load_runner()
    losing_signal = signal(
        runner, 0, entry_bid="0.19", entry_ask="0.20",
        exit_bid="0.10", exit_ask="0.11",
    )
    analysis = runner.affordability_analysis(
        (losing_signal,), policy(runner, commission="0.65")
    )
    assert analysis.minimum_capital_for_participation == Decimal("120.00")
    assert analysis.minimum_economically_viable_capital is None
    assert analysis.economic_viability_curve[0].net_profit < 0


def test_deterministic_summary_and_output_schema():
    runner = load_runner()
    evidence_signals = (signal(runner, 0), signal(runner, 1))
    fixture = runner.Q1CapitalMatrixEvidence(
        schema_version="KAIRO-Q1-CAPITAL-EVIDENCE-v1",
        qualification_manifest_sha256="a" * 64,
        normalized_dataset_manifest_sha256="c" * 64,
        strategy_id="EMA-CROSS-001",
        strategy_version="1.0.0",
        start_session=date(2024, 1, 2), end_session=date(2024, 3, 28),
        artifacts=(runner.ArtifactReference(
            uri="canonical.json", content_sha256="d" * 64, byte_size=1
        ),),
        signals=evidence_signals,
    )
    kwargs = dict(
        manifest_sha256="a" * 64, evidence_sha256="e" * 64,
        capitals=runner.DEFAULT_CAPITALS, policy=policy(runner, slippage="5"),
    )
    first = runner.build_summary(fixture, **kwargs)
    second = runner.build_summary(fixture, **kwargs)
    assert runner.canonical_summary_bytes(first) == runner.canonical_summary_bytes(second)
    assert runner.CapitalMatrixSummary.model_validate_json(
        runner.canonical_summary_bytes(first)
    ) == first
    assert first.cell_count == 1
    assert first.pass_2_status == "BLOCKED_PENDING_CERTIFICATION"
    assert len(first.pass_2_prerequisites) == 4
    assert "| Capital | Signals |" in runner.markdown_summary(first)


@pytest.mark.parametrize("failure", ("manifest_hash", "verdict", "artifact_hash"))
def test_manifest_and_artifact_gate_fails_closed(tmp_path, failure):
    runner = load_runner()
    fixture = sealed_fixture(tmp_path, runner, (signal(runner, 0),), verdict=(
        QualificationStatus.FAIL if failure == "verdict" else QualificationStatus.PASS
    ))
    if failure == "manifest_hash":
        fixture["manifest_sha256"] = "0" * 64
    elif failure == "artifact_hash":
        fixture["artifact_path"].write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        runner.load_certified_evidence(**{
            key: fixture[key]
            for key in ("manifest_uri", "manifest_sha256", "evidence_uri", "evidence_sha256")
        })


def test_missing_evidence_and_scratch_paths_fail_closed(tmp_path):
    runner = load_runner()
    with pytest.raises(FileNotFoundError):
        runner.read_uri(str(tmp_path / "missing.json"))
    with pytest.raises(ValueError, match="scratch"):
        runner.read_uri(str(tmp_path / ".attempt-4-staging-v1" / "artifact.bin"))


def test_non_q1_manifest_window_fails_closed_before_replay(tmp_path):
    runner = load_runner()
    fixture = sealed_fixture(tmp_path, runner, (signal(runner, 0),))
    manifest_path = Path(fixture["manifest_uri"])
    manifest = CorpusQualificationManifest.model_validate_json(manifest_path.read_bytes())
    invalid_window = manifest.pilot_window.model_copy(
        update={"start_session": date(2024, 1, 3)}
    )
    content = manifest.model_copy(update={"pilot_window": invalid_window}).canonical_bytes()
    manifest_path.write_bytes(content)
    fixture["manifest_sha256"] = hashlib.sha256(content).hexdigest()
    with pytest.raises(ValueError, match="certified Q1"):
        runner.load_certified_evidence(**{
            key: fixture[key]
            for key in ("manifest_uri", "manifest_sha256", "evidence_uri", "evidence_sha256")
        })


def test_two_losses_trigger_frozen_strategy_halt_for_session():
    runner = load_runner()
    losing = tuple(
        signal(
            runner, index, entry_bid="0.09", entry_ask="0.10",
            exit_bid="0.04", exit_ask="0.05",
        )
        for index in range(3)
    )
    result = runner.run_tier(Decimal("100"), losing, policy(runner))
    assert result.trades_entered == 2
    assert result.losses == 2
    assert result.risk_halt_rejections == 1
    assert result.hard_halt_sessions == 1


def test_empirical_exit_must_satisfy_frozen_price_threshold():
    runner = load_runner()
    with pytest.raises(ValueError, match="take-profit"):
        runner.EmpiricalSignal(
            signal_id="bad-exit", symbol="TQQQ", session=date(2024, 1, 2),
            signal_at=datetime(2024, 1, 2, 15, 0, tzinfo=UTC),
            exit_at=datetime(2024, 1, 2, 15, 5, tzinfo=UTC),
            entry_bid=Decimal("0.19"), entry_ask=Decimal("0.20"),
            exit_bid=Decimal("0.21"), exit_ask=Decimal("0.22"),
            exit_reason="TAKE_PROFIT",
        )


def test_flywheel_on_is_hard_blocked_before_evidence_access():
    runner = load_runner()
    with pytest.raises(RuntimeError, match="Pass 2 flywheel execution is blocked"):
        runner.main([
            "--manifest-uri", "missing", "--manifest-sha256", "0" * 64,
            "--evidence-uri", "missing", "--evidence-sha256", "0" * 64,
            "--flywheel", "on",
        ])


def test_cli_writes_matching_json_and_markdown_from_local_fixture(tmp_path):
    runner = load_runner()
    fixture = sealed_fixture(tmp_path, runner, (signal(runner, 0),))
    output_json = tmp_path / "q1_capital_matrix_summary.json"
    output_markdown = tmp_path / "q1_capital_matrix_summary.md"
    assert runner.main([
        "--manifest-uri", fixture["manifest_uri"],
        "--manifest-sha256", fixture["manifest_sha256"],
        "--evidence-uri", fixture["evidence_uri"],
        "--evidence-sha256", fixture["evidence_sha256"],
        "--capital", "100", "--capital", "250", "--capital", "500", "--capital", "1000",
        "--commission-per-contract-side", "0.65",
        "--spread-capture-pct", "100", "--slippage-bps", "5",
        "--output-json", str(output_json), "--output-markdown", str(output_markdown),
    ]) == 0
    summary = runner.CapitalMatrixSummary.model_validate_json(output_json.read_bytes())
    assert [tier.starting_capital for tier in summary.tiers] == [
        Decimal("100"), Decimal("250"), Decimal("500"), Decimal("1000")
    ]
    assert output_markdown.read_text(encoding="utf-8").startswith(
        "# Q1 2024 Capital Matrix — Pass 1"
    )
