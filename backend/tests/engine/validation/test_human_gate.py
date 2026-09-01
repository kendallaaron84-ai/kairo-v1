import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.models.configuration import StrategyRegistry
from app.db.models.historical import HistoricalMarketDataset
from app.db.models.projections import CapitalCell
from app.db.models.scorecards import HistoricalValidationAcceptanceFact, HistoricalValidationConfidenceLedger, HistoricalValidationRun
from engine.validation.human_gate import HumanValidationGateService, decision_manifest_sha256


pytestmark = pytest.mark.integration
UTC = timezone.utc
NOW = datetime(2026, 9, 1, 20, 0, tzinfo=UTC)
START = datetime(2019, 1, 1, tzinfo=UTC)
END = datetime(2025, 1, 2, tzinfo=UTC)
POLICY_ID = UUID("a0000000-0000-0000-0000-000000000001")
FACTORS = ("sample_size", "regime_coverage", "data_completeness", "execution_realism", "oos_stability", "profit_distribution", "context_alignment")


def uid(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"kairo-step45-5:{value}")


def factor_values() -> dict:
    values = {}
    for factor in FACTORS:
        values.update({f"{factor}_score": Decimal("95.00"), f"{factor}_status": "SUFFICIENT", f"{factor}_count": 500, f"{factor}_reasons": []})
    return values


class HumanGateFactory:
    def __init__(self, session: Session):
        self.session = session
        self.strategy = session.get(StrategyRegistry, ("EMA-CROSS-001", "1.0.0")); assert self.strategy

    def create(self, label: str, *, score: Decimal = Decimal("98.83"), hard_passed: bool = True, eligible: bool = True):
        dataset_hash = hashlib.sha256(f"{label}:dataset".encode()).hexdigest(); scorecard_hash = hashlib.sha256(f"{label}:scorecard".encode()).hexdigest(); multi_hash = hashlib.sha256(f"{label}:multi".encode()).hexdigest(); confidence_hash = hashlib.sha256(f"{label}:confidence".encode()).hexdigest()
        dataset = HistoricalMarketDataset(dataset_id=uid(f"{label}:dataset"), dataset_name=f"HUMAN-{label}", provider_name="FIXTURE", bar_interval_seconds=60, source_timezone="UTC", calendar_name="XNYS", calendar_version="v1", source_timestamp_convention="INTERVAL_BEGIN", liquidity_fidelity_tier="TIER_1_QUOTE_DEPTH", price_adjustment_mode="RAW_UNADJUSTED", adjustment_policy_version=None, normalization_policy_version="NORM-v1", coverage_start=START, coverage_end=END, dataset_manifest_sha256=dataset_hash, ingested_at=NOW)
        cell = CapitalCell(cell_id=uid(f"{label}:cell"), cell_code=f"HUMAN-{label}", seed_capital=Decimal("100.00"), status="ACTIVE", autonomy_tier="APPRENTICE", strategy_id=self.strategy.strategy_id, strategy_version=self.strategy.version_tag, target_treasury_code="CONF", risk_policy_id=POLICY_ID, economic_domain="SYNTHETIC", updated_at=NOW)
        self.session.add_all([dataset, cell]); self.session.flush()
        run = HistoricalValidationRun(validation_run_id=uid(f"{label}:run"), cell_id=cell.cell_id, dataset_id=dataset.dataset_id, validation_scope="CONSOLIDATED", regime_policy_version="REGIME-POLICY-MULTI-v1", normalization_policy_version="ZSCORE-NORM-v1", sample_start_time=START, sample_end_time=END, total_sessions_count=600, total_trades_count=1000, winning_trades_count=600, losing_trades_count=350, breakeven_trades_count=50, win_rate_pct=Decimal("60.00"), gross_profit_usd=Decimal("10000.00"), gross_loss_usd=Decimal("2000.00"), net_realized_pnl_usd=Decimal("8000.00"), profit_factor=Decimal("5.0000"), expectancy_per_trade_usd=Decimal("8.0000"), max_drawdown_usd=Decimal("100.00"), hard_halt_count=0, longest_losing_streak=3, siphoned_safety_usd=Decimal("100.00"), siphoned_treasury_usd=Decimal("100.00"), siphoned_replication_usd=Decimal("50.00"), cells_spawned_count=1, multi_year_manifest_sha256=multi_hash, scorecard_manifest_sha256=scorecard_hash, executed_at=NOW)
        self.session.add(run); self.session.flush()
        tier = "HIGH_CONFIDENCE" if score >= 80 else "MODERATE_CONFIDENCE" if score >= 65 else "LOW_CONFIDENCE"
        ledger = HistoricalValidationConfidenceLedger(confidence_ledger_id=uid(f"{label}:ledger"), validation_run_id=run.validation_run_id, confidence_policy_version="CONFIDENCE-POLICY-7FACTOR-v2", liquidity_fidelity_tier="TIER_1_QUOTE_DEPTH", composite_confidence_score=score, confidence_tier=tier, hard_gate_passed=hard_passed, gate_eligible=eligible, hard_gate_evaluations_json={"all_six": {"passed": hard_passed}}, confidence_manifest_sha256=confidence_hash, evaluated_at=NOW, **factor_values())
        self.session.add(ledger); self.session.flush()
        return run, ledger, HumanValidationGateService(self.session)


@pytest.fixture
def human_gate_factory(db_session: Session):
    return HumanGateFactory(db_session)


def test_human_gate_records_acceptance_when_eligible_and_authorized(human_gate_factory):
    run, ledger, service = human_gate_factory.create("accepted")
    result = service.record_human_decision(validation_run_id=run.validation_run_id, human_reviewer_identity="governance@example.com", acceptance_decision="ACCEPTED_FOR_LIVE", decision_rationale="Evidence and all independent hard gates reviewed.", decided_at=NOW)
    persisted = human_gate_factory.session.get(HistoricalValidationAcceptanceFact, result.acceptance_fact_id)
    assert persisted.acceptance_decision == "ACCEPTED_FOR_LIVE" and persisted.confidence_ledger_id == ledger.confidence_ledger_id
    assert result.review_package.gate_eligible and result.review_package.hard_gate_passed


def test_human_gate_rejects_acceptance_for_live_when_gate_ineligible(human_gate_factory):
    run, _, service = human_gate_factory.create("ineligible", score=Decimal("79.00"), hard_passed=True, eligible=False)
    with pytest.raises(ValueError, match="ACCEPTED_FOR_LIVE"):
        service.record_human_decision(validation_run_id=run.validation_run_id, human_reviewer_identity="reviewer", acceptance_decision="ACCEPTED_FOR_LIVE", decision_rationale="not eligible", decided_at=NOW)


def test_human_gate_rejects_acceptance_for_live_when_hard_gates_fail(human_gate_factory):
    run, _, service = human_gate_factory.create("hard-fail", hard_passed=False, eligible=False)
    with pytest.raises(ValueError, match="hard gates"):
        service.record_human_decision(validation_run_id=run.validation_run_id, human_reviewer_identity="reviewer", acceptance_decision="ACCEPTED_FOR_LIVE", decision_rationale="hard gates failed", decided_at=NOW)


def test_human_gate_allows_rejection_or_conditional_review_regardless_of_score(human_gate_factory):
    decisions = []
    for decision in ("REJECTED", "CONDITIONAL_REVIEW"):
        run, _, service = human_gate_factory.create(decision.lower(), score=Decimal("20.00"), hard_passed=False, eligible=False)
        decisions.append(service.record_human_decision(validation_run_id=run.validation_run_id, human_reviewer_identity="reviewer", acceptance_decision=decision, decision_rationale="Human review remains adverse or incomplete.", decided_at=NOW).acceptance_decision)
    assert decisions == ["REJECTED", "CONDITIONAL_REVIEW"]


def test_decision_manifest_cryptographically_binds_manifests_and_reviewer(human_gate_factory):
    run, _, service = human_gate_factory.create("manifest")
    result = service.record_human_decision(validation_run_id=run.validation_run_id, human_reviewer_identity="alice@example.com", acceptance_decision="ACCEPTED_FOR_LIVE", decision_rationale="Reviewed.", decided_at=NOW)
    assert decision_manifest_sha256(result.decision_manifest_payload) == result.decision_manifest_sha256
    changed = dict(result.decision_manifest_payload); changed["human_reviewer_identity"] = "mallory@example.com"
    assert decision_manifest_sha256(changed) != result.decision_manifest_sha256
    changed = dict(result.decision_manifest_payload); changed["bound_confidence_manifest_sha256"] = "0" * 64
    assert decision_manifest_sha256(changed) != result.decision_manifest_sha256


def test_kairo_runtime_role_cannot_insert_acceptance_facts(migrated_database):
    admin_url, runtime_url = migrated_database; admin = create_engine(admin_url); runtime = create_engine(runtime_url)
    try:
        with admin.connect() as connection:
            assert connection.scalar(text("SELECT has_table_privilege('kairo_runtime','historical_validation_acceptance_facts','SELECT')"))
            assert not connection.scalar(text("SELECT has_table_privilege('kairo_runtime','historical_validation_acceptance_facts','INSERT')"))
            assert connection.scalar(text("SELECT has_table_privilege('kairo_governance_authority','historical_validation_acceptance_facts','SELECT,INSERT')"))
            assert not connection.scalar(text("SELECT has_table_privilege('kairo_governance_authority','historical_validation_acceptance_facts','UPDATE,DELETE')"))
        with runtime.connect() as connection:
            transaction = connection.begin()
            with pytest.raises(DBAPIError, match="permission denied"):
                connection.execute(text("INSERT INTO historical_validation_acceptance_facts DEFAULT VALUES"))
            transaction.rollback()
    finally: admin.dispose(); runtime.dispose()


def test_immutability_trigger_rejects_update_and_delete_on_acceptance_facts(human_gate_factory):
    run, _, service = human_gate_factory.create("immutable")
    result = service.record_human_decision(validation_run_id=run.validation_run_id, human_reviewer_identity="reviewer", acceptance_decision="REJECTED", decision_rationale="Rejected.", decided_at=NOW)
    for statement in ("UPDATE historical_validation_acceptance_facts SET acceptance_decision=acceptance_decision WHERE acceptance_fact_id=:id", "DELETE FROM historical_validation_acceptance_facts WHERE acceptance_fact_id=:id"):
        with pytest.raises(DBAPIError, match="[Ii]mmutable"):
            with human_gate_factory.session.begin_nested(): human_gate_factory.session.execute(text(statement), {"id": result.acceptance_fact_id})


def test_migration_0026_downgrade_fails_closed_when_data_exists(migrated_database):
    engine = create_engine(migrated_database[0]); backend_root = Path(__file__).resolve().parents[3]; config = Config(str(backend_root / "alembic.ini")); config.set_main_option("script_location", str(backend_root / "alembic"))
    cell_id, dataset_id, run_id, ledger_id, fact_id = (uid(f"downgrade:{name}") for name in ("cell", "dataset", "run", "ledger", "fact")); factors = ",".join(f"{name}_score,{name}_status,{name}_count,{name}_reasons" for name in FACTORS); factor_data = ",".join("95,'SUFFICIENT',500,ARRAY[]::varchar[]" for _ in FACTORS)
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO capital_cells (cell_id,cell_code,seed_capital,status,autonomy_tier,strategy_id,strategy_version,target_treasury_code,risk_policy_id,economic_domain,updated_at) VALUES (:cell,'HUMAN-DOWNGRADE',0,'ACTIVE','APPRENTICE','EMA-CROSS-001','1.0.0','CONF',:policy,'SYNTHETIC',:now)"), {"cell": cell_id, "policy": POLICY_ID, "now": NOW})
            connection.execute(text("INSERT INTO historical_market_datasets (dataset_id,dataset_name,provider_name,bar_interval_seconds,source_timezone,calendar_name,calendar_version,source_timestamp_convention,liquidity_fidelity_tier,price_adjustment_mode,adjustment_policy_version,normalization_policy_version,coverage_start,coverage_end,dataset_manifest_sha256,ingested_at) VALUES (:dataset,'HUMAN-DOWNGRADE','FIXTURE',60,'UTC','XNYS','v1','INTERVAL_BEGIN','TIER_1_QUOTE_DEPTH','RAW_UNADJUSTED',NULL,'NORM-v1',:start,:end,:hash,:now)"), {"dataset": dataset_id, "start": START, "end": END, "hash": "d" * 64, "now": NOW})
            connection.execute(text("INSERT INTO historical_validation_runs (validation_run_id,cell_id,dataset_id,validation_scope,regime_policy_version,normalization_policy_version,sample_start_time,sample_end_time,total_sessions_count,total_trades_count,winning_trades_count,losing_trades_count,breakeven_trades_count,win_rate_pct,gross_profit_usd,gross_loss_usd,net_realized_pnl_usd,profit_factor,expectancy_per_trade_usd,max_drawdown_usd,hard_halt_count,longest_losing_streak,siphoned_safety_usd,siphoned_treasury_usd,siphoned_replication_usd,cells_spawned_count,multi_year_manifest_sha256,scorecard_manifest_sha256,executed_at) VALUES (:run,:cell,:dataset,'DOWNGRADE','REGIME-POLICY-MULTI-v1','ZSCORE-NORM-v1',:start,:end,600,1000,600,350,50,60,10000,2000,8000,5,8,100,0,3,100,100,50,1,:multi,:scorecard,:now)"), {"run": run_id, "cell": cell_id, "dataset": dataset_id, "start": START, "end": END, "multi": "e" * 64, "scorecard": "c" * 64, "now": NOW})
            connection.execute(text(f"INSERT INTO historical_validation_confidence_ledgers (confidence_ledger_id,validation_run_id,confidence_policy_version,liquidity_fidelity_tier,{factors},composite_confidence_score,confidence_tier,hard_gate_passed,gate_eligible,hard_gate_evaluations_json,confidence_manifest_sha256,evaluated_at) VALUES (:ledger,:run,'CONFIDENCE-POLICY-7FACTOR-v2','TIER_1_QUOTE_DEPTH',{factor_data},98.83,'HIGH_CONFIDENCE',true,true,'{{}}',:confidence,:now)"), {"ledger": ledger_id, "run": run_id, "confidence": "f" * 64, "now": NOW})
            connection.execute(text("INSERT INTO historical_validation_acceptance_facts (acceptance_fact_id,confidence_ledger_id,validation_run_id,human_reviewer_identity,acceptance_decision,decision_rationale,confidence_score_at_review,hard_gates_passed_at_review,gate_eligibility_at_review,bound_confidence_manifest_sha256,bound_scorecard_manifest_sha256,bound_multi_year_manifest_sha256,decision_manifest_sha256,decided_at) VALUES (:fact,:ledger,:run,'reviewer','ACCEPTED_FOR_LIVE','reviewed',98.83,true,true,:confidence,:scorecard,:multi,:decision,:now)"), {"fact": fact_id, "ledger": ledger_id, "run": run_id, "confidence": "f" * 64, "scorecard": "c" * 64, "multi": "e" * 64, "decision": "a" * 64, "now": NOW})
        with pytest.raises(Exception, match="Downgrade failed closed"): command.downgrade(config, "0025")
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE historical_validation_acceptance_facts DISABLE TRIGGER trg_historical_validation_acceptance_immutable")); connection.execute(text("DELETE FROM historical_validation_acceptance_facts WHERE acceptance_fact_id=:fact"), {"fact": fact_id})
        command.downgrade(config, "0025"); assert "historical_validation_acceptance_facts" not in inspect(engine).get_table_names()
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE historical_validation_confidence_ledgers DISABLE TRIGGER trg_historical_validation_confidence_immutable")); connection.execute(text("DELETE FROM historical_validation_confidence_ledgers WHERE confidence_ledger_id=:ledger"), {"ledger": ledger_id}); connection.execute(text("ALTER TABLE historical_validation_confidence_ledgers ENABLE TRIGGER trg_historical_validation_confidence_immutable"))
            connection.execute(text("ALTER TABLE historical_validation_runs DISABLE TRIGGER trg_historical_validation_runs_immutable")); connection.execute(text("DELETE FROM historical_validation_runs WHERE validation_run_id=:run"), {"run": run_id}); connection.execute(text("ALTER TABLE historical_validation_runs ENABLE TRIGGER trg_historical_validation_runs_immutable"))
            connection.execute(text("ALTER TABLE historical_market_datasets DISABLE TRIGGER trg_historical_market_datasets_immutable")); connection.execute(text("DELETE FROM historical_market_datasets WHERE dataset_id=:dataset"), {"dataset": dataset_id}); connection.execute(text("ALTER TABLE historical_market_datasets ENABLE TRIGGER trg_historical_market_datasets_immutable")); connection.execute(text("DELETE FROM capital_cells WHERE cell_id=:cell"), {"cell": cell_id})
        command.upgrade(config, "head")
    finally: engine.dispose()
