import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.db.models.configuration import Instrument, StrategyRegistry
from app.db.models.historical import HistoricalMarketDataset
from app.db.models.projections import CapitalCell
from app.db.models.scorecards import (
    HistoricalRunAnalogVector,
    HistoricalSessionDistributionFact,
    HistoricalValidationPerformanceBand,
    HistoricalValidationRegimeSlice,
    HistoricalValidationRun,
)
from engine.validation.regime_policy import RegimeObservation, RegimePolicyV1
from engine.validation.scorecard_engine import SessionPerformanceFact, ValidationScorecardEngine
from engine.validation.vector_normalizer import VectorNormalizer


pytestmark = pytest.mark.integration
UTC = timezone.utc
NOW = datetime(2026, 9, 1, 18, 0, tzinfo=UTC)
POLICY_ID = UUID("a0000000-0000-0000-0000-000000000001")


def uid(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"kairo-step45-3:{value}")


@pytest.fixture
def scorecard_case(db_session: Session):
    strategy = db_session.get(StrategyRegistry, ("EMA-CROSS-001", "1.0.0")); assert strategy
    instrument = Instrument(instrument_id=uid("instrument"), symbol="SCORE", asset_class="EQUITY", currency="USD", effective_from=NOW)
    cell = CapitalCell(cell_id=uid("cell"), cell_code="SCORE-A001", seed_capital=Decimal("100.00"), status="ACTIVE", autonomy_tier="APPRENTICE", strategy_id=strategy.strategy_id, strategy_version=strategy.version_tag, target_treasury_code="SCORE", risk_policy_id=POLICY_ID, economic_domain="SYNTHETIC", updated_at=NOW)
    dataset = HistoricalMarketDataset(dataset_id=uid("dataset"), dataset_name="SCORECARD-FIXTURE", provider_name="FIXTURE", bar_interval_seconds=60, source_timezone="America/New_York", calendar_name="XNYS", calendar_version="CAL-US-EQUITIES-2026-v1", source_timestamp_convention="INTERVAL_BEGIN", liquidity_fidelity_tier="TIER_3_BAR_ONLY", price_adjustment_mode="RAW_UNADJUSTED", adjustment_policy_version=None, normalization_policy_version="NORM-BAR-UTC-CAUSAL-v1", coverage_start=NOW - timedelta(days=20), coverage_end=NOW, dataset_manifest_sha256="2" * 64, ingested_at=NOW)
    db_session.add_all([instrument, cell, dataset]); db_session.flush()
    pnl = ("-5.00", "-3.00", "-1.00", "0.00", "1.00", "2.00", "3.00", "4.00", "5.00", "6.00", "100.00", "8.00")
    facts = tuple(SessionPerformanceFact(
        session_date=date(2026, 8, 1) + timedelta(days=index), session_pnl_usd=Decimal(value), max_drawdown_usd=Decimal(f"{index + 1}.00"),
        trade_count=2, winning_trades_count=1 if Decimal(value) >= 0 else 0, losing_trades_count=0 if Decimal(value) >= 0 else 1, breakeven_trades_count=1,
        hard_halt=index == 2, siphoned_safety_usd=Decimal("0.40"), siphoned_treasury_usd=Decimal("0.40"), siphoned_replication_usd=Decimal("0.20"), cells_spawned_count=1 if index == 11 else 0,
        market_return_pct=Decimal("1.50") if index % 3 == 0 else Decimal("0.20"), vix_level=Decimal("28.00") if index % 2 == 0 else Decimal("14.00"), rate_change_bps=Decimal("30.00") if index == 4 else Decimal("0.00"), event_count=4 if index % 4 == 0 else 0,
    ) for index, value in enumerate(pnl))
    engine = ValidationScorecardEngine(db_session)
    result = engine.evaluate(cell_id=cell.cell_id, dataset_id=dataset.dataset_id, validation_scope="CONSOLIDATED", session_facts=facts, multi_year_manifest_sha256="a" * 64, executed_at=NOW)
    return db_session, cell, dataset, facts, engine, result


def test_early_session_with_insufficient_as_of_history_has_null_percentile_and_band(scorecard_case):
    session, _, _, facts, _, result = scorecard_case
    row = session.scalar(select(HistoricalValidationPerformanceBand).where(HistoricalValidationPerformanceBand.validation_run_id == result.validation_run_id, HistoricalValidationPerformanceBand.session_date == facts[0].session_date))
    assert row.as_of_evidence_status == "INSUFFICIENT_EVIDENCE" and row.as_of_percentile is None and row.as_of_band is None


def test_insufficient_as_of_evidence_is_never_classified_as_critical(scorecard_case):
    session, *_, result = scorecard_case
    rows = session.scalars(select(HistoricalValidationPerformanceBand).where(HistoricalValidationPerformanceBand.validation_run_id == result.validation_run_id, HistoricalValidationPerformanceBand.as_of_evidence_status == "INSUFFICIENT_EVIDENCE")).all()
    assert rows and all(row.as_of_band is None and row.as_of_band != "CRITICAL" for row in rows)


def test_analog_distance_uses_versioned_normalized_features_not_raw_units():
    normalizer = VectorNormalizer(); normalized, params = normalizer.fit_transform([{"price": Decimal("1000"), "rate": Decimal("1")}, {"price": Decimal("2000"), "rate": Decimal("2")}, {"price": Decimal("3000"), "rate": Decimal("3")}])
    assert normalizer.version == "ZSCORE-NORM-v1" and params["price"]["policy_version"] == normalizer.version
    assert normalizer.distance(normalized[0], normalized[1]) == normalizer.distance(normalized[1], normalized[2])


def test_zero_variance_analog_feature_is_handled_deterministically():
    normalizer = VectorNormalizer(); first, params = normalizer.fit_transform([{"constant": Decimal("7")}, {"constant": Decimal("7")}]); second, _ = normalizer.fit_transform([{"constant": Decimal("7")}, {"constant": Decimal("7")}])
    assert first == second == [{"constant": Decimal("0")}, {"constant": Decimal("0")}] and params["constant"]["zero_variance"] is True


def test_regime_classification_is_versioned_and_reproducible():
    policy = RegimePolicyV1(); observation = RegimeObservation(session_date=date(2026, 1, 1), market_return_pct=Decimal("1.20"), vix_level=Decimal("30"), rate_change_bps=Decimal("40"), event_count=4)
    assert policy.version == "REGIME-POLICY-MULTI-v1" and policy.classify(observation) == policy.classify(observation) == ("BULL", "HIGH_VOL", "RATE_SHOCK", "EVENT_HEAVY")


def test_multilabel_regime_sessions_are_not_misrepresented_as_partition_if_allowed(scorecard_case):
    session, *_, result = scorecard_case
    slices = session.scalars(select(HistoricalValidationRegimeSlice).where(HistoricalValidationRegimeSlice.validation_run_id == result.validation_run_id)).all()
    run = session.get(HistoricalValidationRun, result.validation_run_id)
    assert sum(row.sessions_count for row in slices) > run.total_sessions_count


def test_as_of_percentile_excludes_future_sessions_from_benchmark_population(scorecard_case):
    session, _, _, facts, _, result = scorecard_case
    target = session.scalar(select(HistoricalValidationPerformanceBand).where(HistoricalValidationPerformanceBand.validation_run_id == result.validation_run_id, HistoricalValidationPerformanceBand.session_date == facts[10].session_date))
    assert target.as_of_percentile == Decimal("100.00")  # future facts, including the twelfth, are excluded


def test_retrospective_and_as_of_percentiles_are_distinct_facts(scorecard_case):
    session, _, _, facts, _, result = scorecard_case
    target = session.scalar(select(HistoricalValidationPerformanceBand).where(HistoricalValidationPerformanceBand.validation_run_id == result.validation_run_id, HistoricalValidationPerformanceBand.session_date == facts[10].session_date))
    assert target.as_of_percentile is not None and target.retrospective_percentile is not None
    as_of = session.scalar(select(HistoricalSessionDistributionFact).where(HistoricalSessionDistributionFact.validation_run_id == result.validation_run_id, HistoricalSessionDistributionFact.percentile_perspective == "AS_OF", HistoricalSessionDistributionFact.benchmark_as_of_date == facts[10].session_date))
    retrospective = session.scalar(select(HistoricalSessionDistributionFact).where(HistoricalSessionDistributionFact.validation_run_id == result.validation_run_id, HistoricalSessionDistributionFact.percentile_perspective == "RETROSPECTIVE"))
    assert as_of.sample_count == 10 and retrospective.sample_count == 12 and target.as_of_percentile != target.retrospective_percentile


def test_empty_distribution_is_insufficient_evidence_not_zero_performance():
    result = ValidationScorecardEngine.distribution(())
    assert result["distribution_status"] == "INSUFFICIENT_EVIDENCE" and result["sample_count"] == 0 and result["p50_value"] is None and result["mean_value"] is None


def test_distribution_facts_enforce_monotonic_percentiles_when_sufficient(scorecard_case):
    session, *_, result = scorecard_case
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(HistoricalSessionDistributionFact(distribution_id=uid("bad-dist"), validation_run_id=result.validation_run_id, percentile_perspective="AS_OF", benchmark_as_of_date=date(2030, 1, 1), regime_code=None, metric_name="BAD", sample_count=10, distribution_status="SUFFICIENT", p10_value=Decimal("2"), p25_value=Decimal("1"), p50_value=Decimal("3"), p75_value=Decimal("4"), p90_value=Decimal("5"), p99_value=Decimal("6"), mean_value=Decimal("3"), std_dev_value=Decimal("1"))); session.flush()


def test_performance_bands_classified_accurately_from_session_percentiles():
    classify = ValidationScorecardEngine.performance_band
    assert [classify(Decimal(value)) for value in ("90", "75", "40", "10", "9.99")] == ["EXCEPTIONAL", "STRONG", "NOMINAL", "COMPROMISED", "CRITICAL"] and classify(None) is None


def test_scorecard_manifest_hash_is_deterministic_and_byte_exact(scorecard_case):
    _, _, _, _, _, result = scorecard_case
    encoded = json.dumps(result.manifest.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert hashlib.sha256(encoded).hexdigest() == result.manifest.scorecard_manifest_sha256


def test_immutability_triggers_reject_update_and_delete_across_all_five_tables(scorecard_case):
    session, *_, result = scorecard_case
    tables = ("historical_validation_runs", "historical_validation_regime_slices", "historical_session_distribution_facts", "historical_validation_performance_bands", "historical_run_analog_vectors")
    for table in tables:
        for verb in (f"UPDATE {table} SET validation_run_id = validation_run_id WHERE validation_run_id = :run_id", f"DELETE FROM {table} WHERE validation_run_id = :run_id"):
            with pytest.raises(DBAPIError, match="immutable intelligence facts"):
                with session.begin_nested(): session.execute(text(verb), {"run_id": result.validation_run_id})


def test_migration_0024_downgrade_fails_closed_when_any_step3_table_contains_data(migrated_database):
    from sqlalchemy import create_engine
    engine = create_engine(migrated_database[0]); backend_root = Path(__file__).resolve().parents[3]; config = Config(str(backend_root / "alembic.ini")); config.set_main_option("script_location", str(backend_root / "alembic"))
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO historical_validation_runs (validation_run_id,cell_id,dataset_id,validation_scope,regime_policy_version,normalization_policy_version,sample_start_time,sample_end_time,total_sessions_count,total_trades_count,winning_trades_count,losing_trades_count,breakeven_trades_count,win_rate_pct,gross_profit_usd,gross_loss_usd,net_realized_pnl_usd,profit_factor,expectancy_per_trade_usd,max_drawdown_usd,hard_halt_count,longest_losing_streak,siphoned_safety_usd,siphoned_treasury_usd,siphoned_replication_usd,cells_spawned_count,multi_year_manifest_sha256,scorecard_manifest_sha256,executed_at) SELECT :id,cell_id,dataset_id,'DOWNGRADE','REGIME-POLICY-MULTI-v1','ZSCORE-NORM-v1',coverage_start,coverage_end,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,:a,:b,:now FROM capital_cells CROSS JOIN historical_market_datasets LIMIT 1"), {"id": uid("downgrade"), "a": "a" * 64, "b": "b" * 64, "now": NOW})
        with pytest.raises(Exception, match="Downgrade failed closed"): command.downgrade(config, "0023")
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE historical_validation_runs DISABLE TRIGGER trg_historical_validation_runs_immutable")); connection.execute(text("DELETE FROM historical_validation_runs WHERE validation_run_id=:id"), {"id": uid("downgrade")})
        command.downgrade(config, "0023"); assert "historical_validation_runs" not in inspect(engine).get_table_names(); command.upgrade(config, "head")
    finally: engine.dispose()


def test_exact_cent_balance_and_siphon_reconciliation_across_scorecard_runs(scorecard_case):
    session, _, _, facts, _, result = scorecard_case; run = session.get(HistoricalValidationRun, result.validation_run_id)
    assert run.siphoned_safety_usd == sum((fact.siphoned_safety_usd for fact in facts), Decimal("0.00")) == Decimal("4.80")
    assert run.siphoned_treasury_usd == Decimal("4.80") and run.siphoned_replication_usd == Decimal("2.40")
    assert run.siphoned_safety_usd + run.siphoned_treasury_usd + run.siphoned_replication_usd == Decimal("12.00")
