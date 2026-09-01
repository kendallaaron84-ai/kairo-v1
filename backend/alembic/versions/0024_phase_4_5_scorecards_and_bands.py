"""Phase 4.5 Step 3 scorecards, bands, and benchmark ledger.

Revision ID: 0024
Revises: 0023
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table("historical_validation_runs",
        sa.Column("validation_run_id",UUID,primary_key=True), sa.Column("cell_id",UUID,sa.ForeignKey("capital_cells.cell_id"),nullable=False),
        sa.Column("dataset_id",UUID,sa.ForeignKey("historical_market_datasets.dataset_id"),nullable=False), sa.Column("validation_scope",sa.String(64),nullable=False),
        sa.Column("regime_policy_version",sa.String(64),nullable=False), sa.Column("normalization_policy_version",sa.String(64),nullable=False),
        sa.Column("sample_start_time",sa.DateTime(timezone=True),nullable=False), sa.Column("sample_end_time",sa.DateTime(timezone=True),nullable=False),
        sa.Column("total_sessions_count",sa.Integer,nullable=False), sa.Column("total_trades_count",sa.Integer,nullable=False),
        sa.Column("winning_trades_count",sa.Integer,nullable=False), sa.Column("losing_trades_count",sa.Integer,nullable=False), sa.Column("breakeven_trades_count",sa.Integer,nullable=False),
        sa.Column("win_rate_pct",sa.Numeric(5,2),nullable=False), sa.Column("gross_profit_usd",sa.Numeric(12,2),nullable=False), sa.Column("gross_loss_usd",sa.Numeric(12,2),nullable=False),
        sa.Column("net_realized_pnl_usd",sa.Numeric(12,2),nullable=False), sa.Column("profit_factor",sa.Numeric(8,4),nullable=False),
        sa.Column("expectancy_per_trade_usd",sa.Numeric(10,4),nullable=False), sa.Column("max_drawdown_usd",sa.Numeric(12,2),nullable=False),
        sa.Column("hard_halt_count",sa.Integer,nullable=False), sa.Column("longest_losing_streak",sa.Integer,nullable=False),
        sa.Column("siphoned_safety_usd",sa.Numeric(12,2),nullable=False), sa.Column("siphoned_treasury_usd",sa.Numeric(12,2),nullable=False), sa.Column("siphoned_replication_usd",sa.Numeric(12,2),nullable=False),
        sa.Column("cells_spawned_count",sa.Integer,nullable=False), sa.Column("multi_year_manifest_sha256",sa.String(64),nullable=False),
        sa.Column("scorecard_manifest_sha256",sa.String(64),nullable=False,unique=True), sa.Column("executed_at",sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint("total_sessions_count >= 0",name="run_sessions_pos"), sa.CheckConstraint("total_trades_count >= 0",name="run_trades_pos"),
        sa.CheckConstraint("sample_end_time >= sample_start_time",name="run_window_valid"), sa.CheckConstraint("win_rate_pct BETWEEN 0 AND 100",name="run_win_rate_range"),
        sa.CheckConstraint("scorecard_manifest_sha256 ~ '^[a-f0-9]{64}$'",name="run_manifest_sha256_format"), sa.CheckConstraint("multi_year_manifest_sha256 ~ '^[a-f0-9]{64}$'",name="run_my_manifest_sha256_format"))
    op.create_index("idx_validation_runs_cell_scope","historical_validation_runs",["cell_id","validation_scope"])
    op.create_table("historical_validation_regime_slices",
        sa.Column("slice_id",UUID,primary_key=True), sa.Column("validation_run_id",UUID,sa.ForeignKey("historical_validation_runs.validation_run_id"),nullable=False),
        sa.Column("regime_code",sa.String(32),nullable=False), sa.Column("sessions_count",sa.Integer,nullable=False), sa.Column("trades_count",sa.Integer,nullable=False),
        sa.Column("winning_trades_count",sa.Integer,nullable=False), sa.Column("losing_trades_count",sa.Integer,nullable=False), sa.Column("net_pnl_usd",sa.Numeric(12,2),nullable=False),
        sa.Column("win_rate_pct",sa.Numeric(5,2),nullable=False), sa.Column("profit_factor",sa.Numeric(8,4),nullable=False), sa.Column("max_drawdown_usd",sa.Numeric(12,2),nullable=False),
        sa.Column("expectancy_per_trade_usd",sa.Numeric(10,4),nullable=False),
        sa.CheckConstraint("regime_code IN ('BULL','BEAR','HIGH_VOL','LOW_VOL','RATE_SHOCK','EVENT_HEAVY','SIDEWAYS')",name="validation_regime_code"),
        sa.CheckConstraint("sessions_count >= 0",name="slice_sessions_pos"), sa.CheckConstraint("trades_count >= 0",name="slice_trades_pos"),
        sa.UniqueConstraint("validation_run_id","regime_code",name="uq_val_run_regime_slice"))
    op.create_index("idx_regime_slices_run","historical_validation_regime_slices",["validation_run_id"])
    op.create_table("historical_session_distribution_facts",
        sa.Column("distribution_id",UUID,primary_key=True), sa.Column("validation_run_id",UUID,sa.ForeignKey("historical_validation_runs.validation_run_id"),nullable=False),
        sa.Column("percentile_perspective",sa.String(32),nullable=False), sa.Column("benchmark_as_of_date",sa.Date), sa.Column("regime_code",sa.String(32)),
        sa.Column("metric_name",sa.String(64),nullable=False), sa.Column("sample_count",sa.Integer,nullable=False), sa.Column("distribution_status",sa.String(32),nullable=False),
        *[sa.Column(name,sa.Numeric(12,4)) for name in ("p10_value","p25_value","p50_value","p75_value","p90_value","p99_value","mean_value","std_dev_value")],
        sa.CheckConstraint("percentile_perspective IN ('RETROSPECTIVE','AS_OF')",name="dist_perspective"),
        sa.CheckConstraint("distribution_status IN ('SUFFICIENT','INSUFFICIENT_EVIDENCE')",name="dist_status"), sa.CheckConstraint("sample_count >= 0",name="dist_sample_count_pos"),
        sa.CheckConstraint("(distribution_status='INSUFFICIENT_EVIDENCE' AND p10_value IS NULL AND p25_value IS NULL AND p50_value IS NULL AND p75_value IS NULL AND p90_value IS NULL AND p99_value IS NULL) OR (distribution_status='SUFFICIENT' AND p99_value>=p90_value AND p90_value>=p75_value AND p75_value>=p50_value AND p50_value>=p25_value AND p25_value>=p10_value)",name="dist_sufficiency_and_monotonicity"),
        sa.UniqueConstraint("validation_run_id","percentile_perspective","benchmark_as_of_date","regime_code","metric_name",name="uq_run_persp_date_regime_metric"))
    op.create_index("idx_session_dist_run_lookup","historical_session_distribution_facts",["validation_run_id","percentile_perspective","benchmark_as_of_date"])
    op.create_table("historical_validation_performance_bands",
        sa.Column("band_entry_id",UUID,primary_key=True), sa.Column("validation_run_id",UUID,sa.ForeignKey("historical_validation_runs.validation_run_id"),nullable=False),
        sa.Column("session_date",sa.Date,nullable=False), sa.Column("session_pnl_usd",sa.Numeric(12,2),nullable=False),
        sa.Column("retrospective_percentile",sa.Numeric(5,2)), sa.Column("retrospective_band",sa.String(32)), sa.Column("as_of_percentile",sa.Numeric(5,2)), sa.Column("as_of_band",sa.String(32)),
        sa.Column("as_of_evidence_status",sa.String(32),nullable=False), sa.Column("regime_labels",postgresql.ARRAY(sa.String(32)),nullable=False),
        sa.CheckConstraint("as_of_evidence_status IN ('SUFFICIENT','INSUFFICIENT_EVIDENCE')",name="band_as_of_status"),
        sa.CheckConstraint("(as_of_evidence_status='INSUFFICIENT_EVIDENCE' AND as_of_percentile IS NULL AND as_of_band IS NULL) OR (as_of_evidence_status='SUFFICIENT' AND as_of_percentile IS NOT NULL AND as_of_band IS NOT NULL)",name="band_as_of_parity"),
        sa.CheckConstraint("retrospective_band IS NULL OR retrospective_band IN ('EXCEPTIONAL','STRONG','NOMINAL','COMPROMISED','CRITICAL')",name="retro_band_category"),
        sa.CheckConstraint("as_of_band IS NULL OR as_of_band IN ('EXCEPTIONAL','STRONG','NOMINAL','COMPROMISED','CRITICAL')",name="as_of_band_category"),
        sa.CheckConstraint("retrospective_percentile IS NULL OR retrospective_percentile BETWEEN 0 AND 100",name="retro_pct_range"),
        sa.CheckConstraint("as_of_percentile IS NULL OR as_of_percentile BETWEEN 0 AND 100",name="as_of_pct_range"),
        sa.UniqueConstraint("validation_run_id","session_date",name="uq_run_session_band"))
    op.create_index("idx_perf_bands_run_date","historical_validation_performance_bands",["validation_run_id","session_date"])
    op.create_table("historical_run_analog_vectors",
        sa.Column("vector_id",UUID,primary_key=True), sa.Column("validation_run_id",UUID,sa.ForeignKey("historical_validation_runs.validation_run_id"),nullable=False),
        sa.Column("session_date",sa.Date,nullable=False), sa.Column("cohort_type",sa.String(16),nullable=False),
        sa.Column("raw_feature_vector_json",postgresql.JSONB,nullable=False), sa.Column("normalized_z_vector_json",postgresql.JSONB,nullable=False), sa.Column("normalization_parameters_json",postgresql.JSONB,nullable=False),
        sa.Column("daily_pnl_usd",sa.Numeric(12,2),nullable=False), sa.Column("max_drawdown_usd",sa.Numeric(12,2),nullable=False), sa.Column("trade_count",sa.Integer,nullable=False), sa.Column("win_rate_pct",sa.Numeric(5,2),nullable=False),
        sa.CheckConstraint("cohort_type IN ('WINNING','LOSING','NEUTRAL')",name="analog_cohort_type"), sa.UniqueConstraint("validation_run_id","session_date",name="uq_run_analog_session"))
    op.create_index("idx_analog_vectors_run_cohort","historical_run_analog_vectors",["validation_run_id","cohort_type"])
    op.execute("""
    CREATE TRIGGER trg_historical_validation_runs_immutable BEFORE UPDATE OR DELETE ON historical_validation_runs FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();
    CREATE TRIGGER trg_historical_validation_regime_slices_immutable BEFORE UPDATE OR DELETE ON historical_validation_regime_slices FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();
    CREATE TRIGGER trg_historical_session_distribution_facts_immutable BEFORE UPDATE OR DELETE ON historical_session_distribution_facts FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();
    CREATE TRIGGER trg_historical_validation_performance_bands_immutable BEFORE UPDATE OR DELETE ON historical_validation_performance_bands FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();
    CREATE TRIGGER trg_historical_run_analog_vectors_immutable BEFORE UPDATE OR DELETE ON historical_run_analog_vectors FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();
    DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='kairo_runtime') THEN
      REVOKE ALL ON historical_validation_runs,historical_validation_regime_slices,historical_session_distribution_facts,historical_validation_performance_bands,historical_run_analog_vectors FROM kairo_runtime;
      GRANT SELECT,INSERT ON historical_validation_runs,historical_validation_regime_slices,historical_session_distribution_facts,historical_validation_performance_bands,historical_run_analog_vectors TO kairo_runtime;
    END IF; END $$;
    """)


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT 1 FROM historical_run_analog_vectors) OR EXISTS(SELECT 1 FROM historical_validation_performance_bands) OR EXISTS(SELECT 1 FROM historical_session_distribution_facts) OR EXISTS(SELECT 1 FROM historical_validation_regime_slices) OR EXISTS(SELECT 1 FROM historical_validation_runs) THEN RAISE EXCEPTION 'Downgrade failed closed: Immutable Step 3 validation records exist'; END IF; END $$;""")
    for table in ("historical_run_analog_vectors","historical_validation_performance_bands","historical_session_distribution_facts","historical_validation_regime_slices","historical_validation_runs"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON {table}")
    for table in ("historical_run_analog_vectors","historical_validation_performance_bands","historical_session_distribution_facts","historical_validation_regime_slices","historical_validation_runs"):
        op.drop_table(table)
