import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.models.configuration import Instrument, StrategyRegistry
from app.db.models.historical import HistoricalMarketArtifact, HistoricalMarketDataset, HistoricalMarketDatasetSymbol
from app.db.models.projections import CapitalCell
from app.db.models.scorecards import HistoricalValidationConfidenceLedger, HistoricalValidationRegimeSlice, HistoricalValidationRun
from engine.validation.confidence_engine import ConfidenceEvidence, EvidenceConfidenceEngine, INSUFFICIENT, SUFFICIENT


pytestmark = pytest.mark.integration
UTC = timezone.utc
NOW = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)
START = datetime(2019, 1, 1, tzinfo=UTC)
END = datetime(2025, 1, 2, tzinfo=UTC)
POLICY_ID = UUID("a0000000-0000-0000-0000-000000000001")
REGIMES = ("BULL", "BEAR", "HIGH_VOL", "LOW_VOL", "RATE_SHOCK", "EVENT_HEAVY", "SIDEWAYS")


def uid(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"kairo-step45-4:{value}")


def default_evidence(**overrides) -> ConfidenceEvidence:
    values = dict(
        expected_observations=10_000,
        observed_observations=9_950,
        gap_count=50,
        lookahead_violation_count=0,
        rth_boundary_adherence_pct=Decimal("100.00"),
        execution_observation_count=1_000,
        execution_quality_pct=Decimal("100.00"),
        oos_session_count=300,
        in_sample_expectancy_usd=Decimal("1.00"),
        oos_expectancy_usd=Decimal("0.95"),
        largest_session_profit_usd=Decimal("100.00"),
        context_evaluation_count=100,
        context_aligned_count=98,
        claimed_dataset_manifest_sha256="d" * 64,
        claimed_scorecard_manifest_sha256="c" * 64,
        claimed_artifact_sha256s=("a" * 64, "b" * 64),
    )
    values.update(overrides)
    return ConfidenceEvidence(**values)


class ConfidenceCaseFactory:
    def __init__(self, session: Session) -> None:
        self.session = session
        strategy = session.get(StrategyRegistry, ("EMA-CROSS-001", "1.0.0")); assert strategy
        self.strategy = strategy
        self.instrument = Instrument(instrument_id=uid("instrument"), symbol="CONF", asset_class="EQUITY", currency="USD", effective_from=NOW)
        session.add(self.instrument); session.flush()

    def create(self, label: str, *, tier: str = "TIER_1_QUOTE_DEPTH", sessions: int = 600, trades: int = 1_000, start: datetime = START, end: datetime = END, gross_profit: Decimal = Decimal("10000.00"), evidence: ConfidenceEvidence | None = None):
        raw_hash = hashlib.sha256(f"{label}:raw".encode()).hexdigest(); norm_hash = hashlib.sha256(f"{label}:norm".encode()).hexdigest(); dataset_hash = hashlib.sha256(f"{label}:dataset".encode()).hexdigest(); score_hash = hashlib.sha256(f"{label}:scorecard".encode()).hexdigest()
        raw = HistoricalMarketArtifact(artifact_id=uid(f"{label}:raw"), artifact_role="RAW_PROVIDER_PAYLOAD", content_sha256=raw_hash, mime_type="text/csv", byte_size=1, storage_uri=f"file:///{label}/raw", created_at=NOW)
        normalized = HistoricalMarketArtifact(artifact_id=uid(f"{label}:norm"), artifact_role="NORMALIZED_RESEARCH_STREAM", content_sha256=norm_hash, mime_type="application/json", byte_size=1, storage_uri=f"file:///{label}/norm", created_at=NOW)
        dataset = HistoricalMarketDataset(dataset_id=uid(f"{label}:dataset"), dataset_name=f"CONF-{label}", provider_name="FIXTURE", bar_interval_seconds=60, source_timezone="UTC", calendar_name="XNYS", calendar_version="v1", source_timestamp_convention="INTERVAL_BEGIN", liquidity_fidelity_tier=tier, price_adjustment_mode="RAW_UNADJUSTED", adjustment_policy_version=None, normalization_policy_version="NORM-v1", coverage_start=start, coverage_end=end, dataset_manifest_sha256=dataset_hash, ingested_at=NOW)
        cell = CapitalCell(cell_id=uid(f"{label}:cell"), cell_code=f"CONF-{label}", seed_capital=Decimal("100.00"), status="ACTIVE", autonomy_tier="APPRENTICE", strategy_id=self.strategy.strategy_id, strategy_version=self.strategy.version_tag, target_treasury_code="CONF", risk_policy_id=POLICY_ID, economic_domain="SYNTHETIC", updated_at=NOW)
        self.session.add_all([raw, normalized, dataset, cell]); self.session.flush()
        self.session.add(HistoricalMarketDatasetSymbol(symbol_entry_id=uid(f"{label}:symbol"), dataset_id=dataset.dataset_id, instrument_id=self.instrument.instrument_id, symbol=self.instrument.symbol, stream_role="UNDERLYING_SIGNAL_BARS", stream_ordinal=0, raw_artifact_id=raw.artifact_id, raw_content_sha256=raw.content_sha256, normalized_artifact_id=normalized.artifact_id, normalized_content_sha256=normalized.content_sha256, bar_count=10_000, first_bar_start_at=start, last_bar_completed_at=end)); self.session.flush()
        run = HistoricalValidationRun(validation_run_id=uid(f"{label}:run"), cell_id=cell.cell_id, dataset_id=dataset.dataset_id, validation_scope="CONSOLIDATED", regime_policy_version="REGIME-POLICY-MULTI-v1", normalization_policy_version="ZSCORE-NORM-v1", sample_start_time=start, sample_end_time=end, total_sessions_count=sessions, total_trades_count=trades, winning_trades_count=600, losing_trades_count=350, breakeven_trades_count=50, win_rate_pct=Decimal("60.00"), gross_profit_usd=gross_profit, gross_loss_usd=Decimal("2000.00"), net_realized_pnl_usd=gross_profit - Decimal("2000.00"), profit_factor=Decimal("5.0000"), expectancy_per_trade_usd=Decimal("8.0000"), max_drawdown_usd=Decimal("100.00"), hard_halt_count=0, longest_losing_streak=3, siphoned_safety_usd=Decimal("100.00"), siphoned_treasury_usd=Decimal("100.00"), siphoned_replication_usd=Decimal("50.00"), cells_spawned_count=1, multi_year_manifest_sha256=hashlib.sha256(f"{label}:multi".encode()).hexdigest(), scorecard_manifest_sha256=score_hash, executed_at=NOW)
        self.session.add(run); self.session.flush()
        self.session.add_all([HistoricalValidationRegimeSlice(slice_id=uid(f"{label}:regime:{regime}"), validation_run_id=run.validation_run_id, regime_code=regime, sessions_count=100, trades_count=150, winning_trades_count=90, losing_trades_count=60, net_pnl_usd=Decimal("100.00"), win_rate_pct=Decimal("60.00"), profit_factor=Decimal("2.0000"), max_drawdown_usd=Decimal("20.00"), expectancy_per_trade_usd=Decimal("1.0000")) for regime in REGIMES]); self.session.flush()
        evidence = evidence or default_evidence()
        updates = {}
        if evidence.claimed_dataset_manifest_sha256 == "d" * 64: updates["claimed_dataset_manifest_sha256"] = dataset_hash
        if evidence.claimed_scorecard_manifest_sha256 == "c" * 64: updates["claimed_scorecard_manifest_sha256"] = score_hash
        if evidence.claimed_artifact_sha256s == ("a" * 64, "b" * 64): updates["claimed_artifact_sha256s"] = (raw_hash, norm_hash)
        evidence = evidence.model_copy(update=updates)
        result = EvidenceConfidenceEngine(self.session).evaluate(validation_run_id=run.validation_run_id, evidence=evidence, evaluated_at=NOW)
        return run, dataset, result


@pytest.fixture
def confidence_factory(db_session: Session):
    return ConfidenceCaseFactory(db_session)


def test_confidence_engine_evaluates_all_seven_factors_with_complete_epistemic_status(confidence_factory):
    _, _, result = confidence_factory.create("complete")
    assert tuple(result.factors) == ("sample_size", "regime_coverage", "data_completeness", "execution_realism", "oos_stability", "profit_distribution", "context_alignment")
    assert all(factor.evidence_status == SUFFICIENT and factor.score is not None for factor in result.factors.values())
    assert len(result.hard_gates) == 6 and result.hard_gate_passed and result.gate_eligible


def test_hard_gate_fails_when_sample_breadth_is_below_minimum_thresholds(confidence_factory):
    _, _, result = confidence_factory.create("breadth", sessions=499, trades=149)
    assert not result.hard_gates["minimum_sample_breadth"]["passed"] and not result.hard_gate_passed and not result.gate_eligible


def test_hard_gate_fails_closed_on_causal_lookahead_or_lineage_corruption(confidence_factory):
    evidence = default_evidence(lookahead_violation_count=1, claimed_scorecard_manifest_sha256="f" * 64)
    _, _, result = confidence_factory.create("causal", evidence=evidence)
    assert not result.hard_gates["causal_integrity"]["passed"] and not result.hard_gates["cryptographic_lineage"]["passed"] and not result.gate_eligible


def test_gate_eligibility_requires_both_high_confidence_score_and_hard_gate_pass(confidence_factory):
    _, _, result = confidence_factory.create("dual-key", tier="TIER_3_BAR_ONLY")
    assert result.composite_confidence_score >= 80 and result.confidence_tier == "HIGH_CONFIDENCE"
    assert not result.hard_gate_passed and not result.gate_eligible


def test_execution_realism_is_strictly_bounded_by_liquidity_fidelity_tier(confidence_factory):
    results = [confidence_factory.create(f"tier-{index}", tier=tier)[2] for index, tier in enumerate(("TIER_1_QUOTE_DEPTH", "TIER_2_TRADE_HISTORY", "TIER_3_BAR_ONLY"), 1)]
    assert [result.factors["execution_realism"].score for result in results] == [Decimal("100.00"), Decimal("75.00"), Decimal("40.00")]


def test_data_completeness_uses_gap_rate_not_absolute_bar_count(confidence_factory):
    small = default_evidence(expected_observations=100, observed_observations=90, gap_count=10)
    large = default_evidence(expected_observations=1_000, observed_observations=900, gap_count=100)
    first = confidence_factory.create("gaps-small", evidence=small)[2]
    second = confidence_factory.create("gaps-large", evidence=large)[2]
    assert first.factors["data_completeness"].score == second.factors["data_completeness"].score == Decimal("90.00")


def test_oos_stability_handles_nonpositive_in_sample_expectancy_without_synthetic_clamps(confidence_factory):
    _, _, result = confidence_factory.create("oos-zero", evidence=default_evidence(in_sample_expectancy_usd=Decimal("0.00")))
    factor = result.factors["oos_stability"]
    assert factor.score == Decimal("0.00") and factor.evidence_status == INSUFFICIENT and factor.reason_codes == ("NON_POSITIVE_IN_SAMPLE_BASELINE",)
    assert not result.hard_gates["oos_evidence_sufficiency"]["passed"] and not result.hard_gates["required_factor_sufficiency"]["passed"]


def test_profit_distribution_requires_positive_gross_profit_baseline(confidence_factory):
    _, _, result = confidence_factory.create("zero-profit", gross_profit=Decimal("0.00"), evidence=default_evidence(largest_session_profit_usd=Decimal("0.00")))
    factor = result.factors["profit_distribution"]
    assert factor.score == Decimal("0.00") and factor.reason_codes == ("ZERO_GROSS_PROFIT_BASELINE",)


def test_missing_context_gate_evidence_records_insufficient_evidence_not_perfect_score(confidence_factory):
    _, _, result = confidence_factory.create("no-context", evidence=default_evidence(context_evaluation_count=0, context_aligned_count=0))
    factor = result.factors["context_alignment"]
    assert factor.score is None and factor.evidence_status == INSUFFICIENT and not result.hard_gates["required_factor_sufficiency"]["passed"]


def test_confidence_manifest_hash_is_deterministic_and_byte_exact(confidence_factory):
    _, _, result = confidence_factory.create("manifest")
    encoded = json.dumps(result.manifest.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert hashlib.sha256(encoded).hexdigest() == result.manifest.confidence_manifest_sha256


def test_immutability_trigger_rejects_update_and_delete_on_confidence_ledger(confidence_factory):
    _, _, result = confidence_factory.create("immutable"); session = confidence_factory.session
    for statement in ("UPDATE historical_validation_confidence_ledgers SET gate_eligible=gate_eligible WHERE confidence_ledger_id=:id", "DELETE FROM historical_validation_confidence_ledgers WHERE confidence_ledger_id=:id"):
        with pytest.raises(DBAPIError, match="[Ii]mmutable"):
            with session.begin_nested(): session.execute(text(statement), {"id": result.confidence_ledger_id})


def test_migration_0025_downgrade_fails_closed_when_data_exists(migrated_database):
    from sqlalchemy import create_engine
    engine = create_engine(migrated_database[0]); backend_root = Path(__file__).resolve().parents[3]; config = Config(str(backend_root / "alembic.ini")); config.set_main_option("script_location", str(backend_root / "alembic"))
    cell_id, dataset_id, run_id, ledger_id = (uid(f"downgrade:{name}") for name in ("cell", "dataset", "run", "ledger"))
    factor_columns = ",".join(f"{name}_score,{name}_status,{name}_count,{name}_reasons" for name in ("sample_size", "regime_coverage", "data_completeness", "execution_realism", "oos_stability", "profit_distribution", "context_alignment"))
    factor_values = ",".join("100,'SUFFICIENT',1,ARRAY[]::varchar[]" for _ in range(7))
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO capital_cells (cell_id,cell_code,seed_capital,status,autonomy_tier,strategy_id,strategy_version,target_treasury_code,risk_policy_id,economic_domain,updated_at) VALUES (:cell,'CONF-DOWNGRADE',0,'ACTIVE','APPRENTICE','EMA-CROSS-001','1.0.0','CONF',:policy,'SYNTHETIC',:now)"), {"cell": cell_id, "policy": POLICY_ID, "now": NOW})
            connection.execute(text("INSERT INTO historical_market_datasets (dataset_id,dataset_name,provider_name,bar_interval_seconds,source_timezone,calendar_name,calendar_version,source_timestamp_convention,liquidity_fidelity_tier,price_adjustment_mode,adjustment_policy_version,normalization_policy_version,coverage_start,coverage_end,dataset_manifest_sha256,ingested_at) VALUES (:dataset,'CONF-DOWNGRADE','FIXTURE',60,'UTC','XNYS','v1','INTERVAL_BEGIN','TIER_1_QUOTE_DEPTH','RAW_UNADJUSTED',NULL,'NORM-v1',:start,:end,:hash,:now)"), {"dataset": dataset_id, "start": START, "end": END, "hash": "d" * 64, "now": NOW})
            connection.execute(text("INSERT INTO historical_validation_runs (validation_run_id,cell_id,dataset_id,validation_scope,regime_policy_version,normalization_policy_version,sample_start_time,sample_end_time,total_sessions_count,total_trades_count,winning_trades_count,losing_trades_count,breakeven_trades_count,win_rate_pct,gross_profit_usd,gross_loss_usd,net_realized_pnl_usd,profit_factor,expectancy_per_trade_usd,max_drawdown_usd,hard_halt_count,longest_losing_streak,siphoned_safety_usd,siphoned_treasury_usd,siphoned_replication_usd,cells_spawned_count,multi_year_manifest_sha256,scorecard_manifest_sha256,executed_at) VALUES (:run,:cell,:dataset,'DOWNGRADE','REGIME-POLICY-MULTI-v1','ZSCORE-NORM-v1',:start,:end,600,1000,600,350,50,60,10000,2000,8000,5,8,100,0,3,100,100,50,1,:multi,:score,:now)"), {"run": run_id, "cell": cell_id, "dataset": dataset_id, "start": START, "end": END, "multi": "e" * 64, "score": "c" * 64, "now": NOW})
            connection.execute(text(f"INSERT INTO historical_validation_confidence_ledgers (confidence_ledger_id,validation_run_id,confidence_policy_version,liquidity_fidelity_tier,{factor_columns},composite_confidence_score,confidence_tier,hard_gate_passed,gate_eligible,hard_gate_evaluations_json,confidence_manifest_sha256,evaluated_at) VALUES (:ledger,:run,'CONFIDENCE-POLICY-7FACTOR-v2','TIER_1_QUOTE_DEPTH',{factor_values},100,'HIGH_CONFIDENCE',true,true,'{{}}',:manifest,:now)"), {"ledger": ledger_id, "run": run_id, "manifest": "f" * 64, "now": NOW})
        with pytest.raises(Exception, match="Downgrade failed closed"): command.downgrade(config, "0024")
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE historical_validation_confidence_ledgers DISABLE TRIGGER trg_historical_validation_confidence_immutable")); connection.execute(text("DELETE FROM historical_validation_confidence_ledgers WHERE confidence_ledger_id=:ledger"), {"ledger": ledger_id})
        command.downgrade(config, "0024"); assert "historical_validation_confidence_ledgers" not in inspect(engine).get_table_names(); command.upgrade(config, "head")
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE historical_validation_runs DISABLE TRIGGER trg_historical_validation_runs_immutable")); connection.execute(text("DELETE FROM historical_validation_runs WHERE validation_run_id=:run"), {"run": run_id}); connection.execute(text("ALTER TABLE historical_validation_runs ENABLE TRIGGER trg_historical_validation_runs_immutable"))
            connection.execute(text("ALTER TABLE historical_market_datasets DISABLE TRIGGER trg_historical_market_datasets_immutable")); connection.execute(text("DELETE FROM historical_market_datasets WHERE dataset_id=:dataset"), {"dataset": dataset_id}); connection.execute(text("ALTER TABLE historical_market_datasets ENABLE TRIGGER trg_historical_market_datasets_immutable")); connection.execute(text("DELETE FROM capital_cells WHERE cell_id=:cell"), {"cell": cell_id})
    finally: engine.dispose()
