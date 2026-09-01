from datetime import date, datetime
from decimal import Decimal
from uuid import UUID
from sqlalchemy import ARRAY, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class HistoricalValidationRun(Base):
    __tablename__="historical_validation_runs"
    __table_args__=(CheckConstraint("total_sessions_count >= 0",name="run_sessions_pos"),CheckConstraint("total_trades_count >= 0",name="run_trades_pos"),CheckConstraint("sample_end_time >= sample_start_time",name="run_window_valid"),CheckConstraint("win_rate_pct BETWEEN 0 AND 100",name="run_win_rate_range"),CheckConstraint("scorecard_manifest_sha256 ~ '^[a-f0-9]{64}$'",name="run_manifest_sha256_format"),CheckConstraint("multi_year_manifest_sha256 ~ '^[a-f0-9]{64}$'",name="run_my_manifest_sha256_format"),Index("idx_validation_runs_cell_scope","cell_id","validation_scope"))
    validation_run_id:Mapped[UUID]=mapped_column(PGUUID(as_uuid=True),primary_key=True); cell_id:Mapped[UUID]=mapped_column(PGUUID(as_uuid=True),ForeignKey("capital_cells.cell_id"),nullable=False); dataset_id:Mapped[UUID]=mapped_column(PGUUID(as_uuid=True),ForeignKey("historical_market_datasets.dataset_id"),nullable=False)
    validation_scope:Mapped[str]=mapped_column(String(64),nullable=False); regime_policy_version:Mapped[str]=mapped_column(String(64),nullable=False); normalization_policy_version:Mapped[str]=mapped_column(String(64),nullable=False)
    sample_start_time:Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False); sample_end_time:Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False)
    total_sessions_count:Mapped[int]=mapped_column(Integer,nullable=False); total_trades_count:Mapped[int]=mapped_column(Integer,nullable=False); winning_trades_count:Mapped[int]=mapped_column(Integer,nullable=False); losing_trades_count:Mapped[int]=mapped_column(Integer,nullable=False); breakeven_trades_count:Mapped[int]=mapped_column(Integer,nullable=False)
    win_rate_pct:Mapped[Decimal]=mapped_column(Numeric(5,2),nullable=False); gross_profit_usd:Mapped[Decimal]=mapped_column(Numeric(12,2),nullable=False); gross_loss_usd:Mapped[Decimal]=mapped_column(Numeric(12,2),nullable=False); net_realized_pnl_usd:Mapped[Decimal]=mapped_column(Numeric(12,2),nullable=False); profit_factor:Mapped[Decimal]=mapped_column(Numeric(8,4),nullable=False); expectancy_per_trade_usd:Mapped[Decimal]=mapped_column(Numeric(10,4),nullable=False); max_drawdown_usd:Mapped[Decimal]=mapped_column(Numeric(12,2),nullable=False)
    hard_halt_count:Mapped[int]=mapped_column(Integer,nullable=False); longest_losing_streak:Mapped[int]=mapped_column(Integer,nullable=False); siphoned_safety_usd:Mapped[Decimal]=mapped_column(Numeric(12,2),nullable=False); siphoned_treasury_usd:Mapped[Decimal]=mapped_column(Numeric(12,2),nullable=False); siphoned_replication_usd:Mapped[Decimal]=mapped_column(Numeric(12,2),nullable=False); cells_spawned_count:Mapped[int]=mapped_column(Integer,nullable=False)
    multi_year_manifest_sha256:Mapped[str]=mapped_column(String(64),nullable=False); scorecard_manifest_sha256:Mapped[str]=mapped_column(String(64),nullable=False,unique=True); executed_at:Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False)


class HistoricalValidationRegimeSlice(Base):
    __tablename__="historical_validation_regime_slices"
    __table_args__=(CheckConstraint("regime_code IN ('BULL','BEAR','HIGH_VOL','LOW_VOL','RATE_SHOCK','EVENT_HEAVY','SIDEWAYS')",name="validation_regime_code"),CheckConstraint("sessions_count >= 0",name="slice_sessions_pos"),CheckConstraint("trades_count >= 0",name="slice_trades_pos"),UniqueConstraint("validation_run_id","regime_code",name="uq_val_run_regime_slice"),Index("idx_regime_slices_run","validation_run_id"))
    slice_id:Mapped[UUID]=mapped_column(PGUUID(as_uuid=True),primary_key=True); validation_run_id:Mapped[UUID]=mapped_column(PGUUID(as_uuid=True),ForeignKey("historical_validation_runs.validation_run_id"),nullable=False); regime_code:Mapped[str]=mapped_column(String(32),nullable=False)
    sessions_count:Mapped[int]=mapped_column(Integer,nullable=False); trades_count:Mapped[int]=mapped_column(Integer,nullable=False); winning_trades_count:Mapped[int]=mapped_column(Integer,nullable=False); losing_trades_count:Mapped[int]=mapped_column(Integer,nullable=False); net_pnl_usd:Mapped[Decimal]=mapped_column(Numeric(12,2),nullable=False); win_rate_pct:Mapped[Decimal]=mapped_column(Numeric(5,2),nullable=False); profit_factor:Mapped[Decimal]=mapped_column(Numeric(8,4),nullable=False); max_drawdown_usd:Mapped[Decimal]=mapped_column(Numeric(12,2),nullable=False); expectancy_per_trade_usd:Mapped[Decimal]=mapped_column(Numeric(10,4),nullable=False)


class HistoricalSessionDistributionFact(Base):
    __tablename__="historical_session_distribution_facts"
    __table_args__=(CheckConstraint("percentile_perspective IN ('RETROSPECTIVE','AS_OF')",name="dist_perspective"),CheckConstraint("distribution_status IN ('SUFFICIENT','INSUFFICIENT_EVIDENCE')",name="dist_status"),CheckConstraint("sample_count >= 0",name="dist_sample_count_pos"),CheckConstraint("(distribution_status='INSUFFICIENT_EVIDENCE' AND p10_value IS NULL AND p25_value IS NULL AND p50_value IS NULL AND p75_value IS NULL AND p90_value IS NULL AND p99_value IS NULL) OR (distribution_status='SUFFICIENT' AND p99_value>=p90_value AND p90_value>=p75_value AND p75_value>=p50_value AND p50_value>=p25_value AND p25_value>=p10_value)",name="dist_sufficiency_and_monotonicity"),UniqueConstraint("validation_run_id","percentile_perspective","benchmark_as_of_date","regime_code","metric_name",name="uq_run_persp_date_regime_metric"),Index("idx_session_dist_run_lookup","validation_run_id","percentile_perspective","benchmark_as_of_date"))
    distribution_id:Mapped[UUID]=mapped_column(PGUUID(as_uuid=True),primary_key=True); validation_run_id:Mapped[UUID]=mapped_column(PGUUID(as_uuid=True),ForeignKey("historical_validation_runs.validation_run_id"),nullable=False); percentile_perspective:Mapped[str]=mapped_column(String(32),nullable=False); benchmark_as_of_date:Mapped[date|None]=mapped_column(Date); regime_code:Mapped[str|None]=mapped_column(String(32)); metric_name:Mapped[str]=mapped_column(String(64),nullable=False); sample_count:Mapped[int]=mapped_column(Integer,nullable=False); distribution_status:Mapped[str]=mapped_column(String(32),nullable=False)
    p10_value:Mapped[Decimal|None]=mapped_column(Numeric(12,4)); p25_value:Mapped[Decimal|None]=mapped_column(Numeric(12,4)); p50_value:Mapped[Decimal|None]=mapped_column(Numeric(12,4)); p75_value:Mapped[Decimal|None]=mapped_column(Numeric(12,4)); p90_value:Mapped[Decimal|None]=mapped_column(Numeric(12,4)); p99_value:Mapped[Decimal|None]=mapped_column(Numeric(12,4)); mean_value:Mapped[Decimal|None]=mapped_column(Numeric(12,4)); std_dev_value:Mapped[Decimal|None]=mapped_column(Numeric(12,4))


class HistoricalValidationPerformanceBand(Base):
    __tablename__="historical_validation_performance_bands"
    __table_args__=(CheckConstraint("as_of_evidence_status IN ('SUFFICIENT','INSUFFICIENT_EVIDENCE')",name="band_as_of_status"),CheckConstraint("(as_of_evidence_status='INSUFFICIENT_EVIDENCE' AND as_of_percentile IS NULL AND as_of_band IS NULL) OR (as_of_evidence_status='SUFFICIENT' AND as_of_percentile IS NOT NULL AND as_of_band IS NOT NULL)",name="band_as_of_parity"),CheckConstraint("retrospective_band IS NULL OR retrospective_band IN ('EXCEPTIONAL','STRONG','NOMINAL','COMPROMISED','CRITICAL')",name="retro_band_category"),CheckConstraint("as_of_band IS NULL OR as_of_band IN ('EXCEPTIONAL','STRONG','NOMINAL','COMPROMISED','CRITICAL')",name="as_of_band_category"),CheckConstraint("retrospective_percentile IS NULL OR retrospective_percentile BETWEEN 0 AND 100",name="retro_pct_range"),CheckConstraint("as_of_percentile IS NULL OR as_of_percentile BETWEEN 0 AND 100",name="as_of_pct_range"),UniqueConstraint("validation_run_id","session_date",name="uq_run_session_band"),Index("idx_perf_bands_run_date","validation_run_id","session_date"))
    band_entry_id:Mapped[UUID]=mapped_column(PGUUID(as_uuid=True),primary_key=True); validation_run_id:Mapped[UUID]=mapped_column(PGUUID(as_uuid=True),ForeignKey("historical_validation_runs.validation_run_id"),nullable=False); session_date:Mapped[date]=mapped_column(Date,nullable=False); session_pnl_usd:Mapped[Decimal]=mapped_column(Numeric(12,2),nullable=False); retrospective_percentile:Mapped[Decimal|None]=mapped_column(Numeric(5,2)); retrospective_band:Mapped[str|None]=mapped_column(String(32)); as_of_percentile:Mapped[Decimal|None]=mapped_column(Numeric(5,2)); as_of_band:Mapped[str|None]=mapped_column(String(32)); as_of_evidence_status:Mapped[str]=mapped_column(String(32),nullable=False); regime_labels:Mapped[list[str]]=mapped_column(ARRAY(String(32)),nullable=False)


class HistoricalRunAnalogVector(Base):
    __tablename__="historical_run_analog_vectors"
    __table_args__=(CheckConstraint("cohort_type IN ('WINNING','LOSING','NEUTRAL')",name="analog_cohort_type"),UniqueConstraint("validation_run_id","session_date",name="uq_run_analog_session"),Index("idx_analog_vectors_run_cohort","validation_run_id","cohort_type"))
    vector_id:Mapped[UUID]=mapped_column(PGUUID(as_uuid=True),primary_key=True); validation_run_id:Mapped[UUID]=mapped_column(PGUUID(as_uuid=True),ForeignKey("historical_validation_runs.validation_run_id"),nullable=False); session_date:Mapped[date]=mapped_column(Date,nullable=False); cohort_type:Mapped[str]=mapped_column(String(16),nullable=False); raw_feature_vector_json:Mapped[dict]=mapped_column(JSONB,nullable=False); normalized_z_vector_json:Mapped[dict]=mapped_column(JSONB,nullable=False); normalization_parameters_json:Mapped[dict]=mapped_column(JSONB,nullable=False); daily_pnl_usd:Mapped[Decimal]=mapped_column(Numeric(12,2),nullable=False); max_drawdown_usd:Mapped[Decimal]=mapped_column(Numeric(12,2),nullable=False); trade_count:Mapped[int]=mapped_column(Integer,nullable=False); win_rate_pct:Mapped[Decimal]=mapped_column(Numeric(5,2),nullable=False)


class HistoricalValidationConfidenceLedger(Base):
    __tablename__ = "historical_validation_confidence_ledgers"
    __table_args__ = (
        CheckConstraint("confidence_tier IN ('HIGH_CONFIDENCE','MODERATE_CONFIDENCE','LOW_CONFIDENCE')", name="confidence_tier_category"),
        CheckConstraint("composite_confidence_score >= 0.00 AND composite_confidence_score <= 100.00", name="composite_score_range"),
        CheckConstraint("(composite_confidence_score >= 80.00 AND confidence_tier = 'HIGH_CONFIDENCE') OR (composite_confidence_score >= 65.00 AND composite_confidence_score < 80.00 AND confidence_tier = 'MODERATE_CONFIDENCE') OR (composite_confidence_score < 65.00 AND confidence_tier = 'LOW_CONFIDENCE')", name="confidence_tier_score_parity"),
        CheckConstraint("(gate_eligible = TRUE AND composite_confidence_score >= 80.00 AND hard_gate_passed = TRUE) OR (gate_eligible = FALSE AND (composite_confidence_score < 80.00 OR hard_gate_passed = FALSE))", name="gate_eligibility_dual_key_parity"),
        CheckConstraint("confidence_manifest_sha256 ~ '^[a-f0-9]{64}$'", name="conf_manifest_sha256_format"),
        UniqueConstraint("validation_run_id", name="uq_conf_validation_run"),
        Index("idx_confidence_gate_lookup", "gate_eligible", "hard_gate_passed", "confidence_tier"),
    )

    confidence_ledger_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    validation_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("historical_validation_runs.validation_run_id"), nullable=False)
    confidence_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    liquidity_fidelity_tier: Mapped[str] = mapped_column(String(32), nullable=False)

    sample_size_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    sample_size_status: Mapped[str] = mapped_column(String(32), nullable=False)
    sample_size_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sample_size_reasons: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    regime_coverage_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    regime_coverage_status: Mapped[str] = mapped_column(String(32), nullable=False)
    regime_coverage_count: Mapped[int] = mapped_column(Integer, nullable=False)
    regime_coverage_reasons: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    data_completeness_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    data_completeness_status: Mapped[str] = mapped_column(String(32), nullable=False)
    data_completeness_count: Mapped[int] = mapped_column(Integer, nullable=False)
    data_completeness_reasons: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    execution_realism_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    execution_realism_status: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_realism_count: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_realism_reasons: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    oos_stability_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    oos_stability_status: Mapped[str] = mapped_column(String(32), nullable=False)
    oos_stability_count: Mapped[int] = mapped_column(Integer, nullable=False)
    oos_stability_reasons: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    profit_distribution_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    profit_distribution_status: Mapped[str] = mapped_column(String(32), nullable=False)
    profit_distribution_count: Mapped[int] = mapped_column(Integer, nullable=False)
    profit_distribution_reasons: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    context_alignment_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    context_alignment_status: Mapped[str] = mapped_column(String(32), nullable=False)
    context_alignment_count: Mapped[int] = mapped_column(Integer, nullable=False)
    context_alignment_reasons: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)

    composite_confidence_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    confidence_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    hard_gate_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    gate_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    hard_gate_evaluations_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confidence_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HistoricalValidationAcceptanceFact(Base):
    __tablename__ = "historical_validation_acceptance_facts"
    __table_args__ = (
        CheckConstraint("acceptance_decision IN ('ACCEPTED_FOR_LIVE','REJECTED','CONDITIONAL_REVIEW')", name="acceptance_decision_type"),
        CheckConstraint("(acceptance_decision = 'ACCEPTED_FOR_LIVE' AND gate_eligibility_at_review = TRUE AND hard_gates_passed_at_review = TRUE AND confidence_score_at_review >= 80.00) OR (acceptance_decision IN ('REJECTED','CONDITIONAL_REVIEW'))", name="human_acceptance_prerequisite_parity"),
        CheckConstraint("bound_confidence_manifest_sha256 ~ '^[a-f0-9]{64}$'", name="bound_conf_sha_format"),
        CheckConstraint("bound_scorecard_manifest_sha256 ~ '^[a-f0-9]{64}$'", name="bound_scorecard_sha_format"),
        CheckConstraint("bound_multi_year_manifest_sha256 ~ '^[a-f0-9]{64}$'", name="bound_multi_year_sha_format"),
        CheckConstraint("decision_manifest_sha256 ~ '^[a-f0-9]{64}$'", name="decision_manifest_sha_format"),
        UniqueConstraint("validation_run_id", name="uq_run_human_acceptance"),
        Index("idx_acceptance_decision_lookup", "acceptance_decision", "decided_at"),
    )

    acceptance_fact_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    confidence_ledger_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("historical_validation_confidence_ledgers.confidence_ledger_id"), nullable=False)
    validation_run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("historical_validation_runs.validation_run_id"), nullable=False)
    human_reviewer_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    acceptance_decision: Mapped[str] = mapped_column(String(32), nullable=False)
    decision_rationale: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_score_at_review: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    hard_gates_passed_at_review: Mapped[bool] = mapped_column(Boolean, nullable=False)
    gate_eligibility_at_review: Mapped[bool] = mapped_column(Boolean, nullable=False)
    bound_confidence_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    bound_scorecard_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    bound_multi_year_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
