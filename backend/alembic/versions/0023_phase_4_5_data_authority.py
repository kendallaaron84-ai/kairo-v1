"""Phase 4.5 Step 1: canonical historical market data authority.

Revision ID: 0023
Revises: 0022
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "historical_market_artifacts",
        sa.Column("artifact_id", UUID, primary_key=True),
        sa.Column("artifact_role", sa.String(32), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("mime_type", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("storage_uri", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("artifact_role IN ('RAW_PROVIDER_PAYLOAD','NORMALIZED_RESEARCH_STREAM')", name="market_artifact_role"),
        sa.CheckConstraint("byte_size > 0", name="market_artifact_size_pos"),
        sa.CheckConstraint("content_sha256 ~ '^[a-f0-9]{64}$'", name="market_artifact_sha256_format"),
    )
    op.create_index("idx_market_artifacts_hash", "historical_market_artifacts", ["content_sha256"])
    op.create_table(
        "historical_market_datasets",
        sa.Column("dataset_id", UUID, primary_key=True),
        sa.Column("dataset_name", sa.String(128), nullable=False),
        sa.Column("provider_name", sa.String(64), nullable=False),
        sa.Column("bar_interval_seconds", sa.Integer(), nullable=False),
        sa.Column("source_timezone", sa.String(64), nullable=False),
        sa.Column("calendar_name", sa.String(64), nullable=False),
        sa.Column("calendar_version", sa.String(64), nullable=False),
        sa.Column("source_timestamp_convention", sa.String(32), nullable=False),
        sa.Column("liquidity_fidelity_tier", sa.String(32), nullable=False),
        sa.Column("price_adjustment_mode", sa.String(32), nullable=False),
        sa.Column("adjustment_policy_version", sa.String(64)),
        sa.Column("normalization_policy_version", sa.String(64), nullable=False),
        sa.Column("coverage_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("coverage_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dataset_manifest_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("bar_interval_seconds > 0", name="dataset_bar_interval_pos"),
        sa.CheckConstraint("source_timestamp_convention IN ('INTERVAL_BEGIN','INTERVAL_END','TICK_ARRIVAL')", name="dataset_timestamp_convention"),
        sa.CheckConstraint("liquidity_fidelity_tier IN ('TIER_1_QUOTE_DEPTH','TIER_2_TRADE_HISTORY','TIER_3_BAR_ONLY')", name="dataset_fidelity_tier"),
        sa.CheckConstraint("price_adjustment_mode IN ('RAW_UNADJUSTED','SPLIT_ADJUSTED_SERIES','SPLIT_AND_DIVIDEND_ADJUSTED')", name="dataset_price_adj_mode"),
        sa.CheckConstraint("(price_adjustment_mode = 'RAW_UNADJUSTED' AND adjustment_policy_version IS NULL) OR (price_adjustment_mode <> 'RAW_UNADJUSTED' AND adjustment_policy_version IS NOT NULL)", name="dataset_adj_version_parity"),
        sa.CheckConstraint("coverage_end >= coverage_start", name="dataset_coverage_valid"),
        sa.CheckConstraint("dataset_manifest_sha256 ~ '^[a-f0-9]{64}$'", name="dataset_manifest_sha256_format"),
    )
    op.create_index("idx_datasets_provider_cov", "historical_market_datasets", ["provider_name", "coverage_start", "coverage_end"])
    op.create_table(
        "historical_market_dataset_symbols",
        sa.Column("symbol_entry_id", UUID, primary_key=True),
        sa.Column("dataset_id", UUID, sa.ForeignKey("historical_market_datasets.dataset_id"), nullable=False),
        sa.Column("instrument_id", UUID, sa.ForeignKey("instruments.instrument_id"), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("stream_role", sa.String(32), nullable=False),
        sa.Column("stream_ordinal", sa.Integer(), nullable=False),
        sa.Column("raw_artifact_id", UUID, sa.ForeignKey("historical_market_artifacts.artifact_id"), nullable=False),
        sa.Column("raw_content_sha256", sa.String(64), nullable=False),
        sa.Column("normalized_artifact_id", UUID, sa.ForeignKey("historical_market_artifacts.artifact_id"), nullable=False),
        sa.Column("normalized_content_sha256", sa.String(64), nullable=False),
        sa.Column("bar_count", sa.BigInteger(), nullable=False),
        sa.Column("first_bar_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_bar_completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("stream_role IN ('UNDERLYING_SIGNAL_BARS','OPTION_CHAIN_QUOTES','CONTEXT_MACRO_SERIES')", name="symbol_stream_role"),
        sa.CheckConstraint("stream_ordinal >= 0", name="symbol_stream_ordinal_pos"),
        sa.CheckConstraint("bar_count > 0", name="symbol_bar_count_pos"),
        sa.CheckConstraint("last_bar_completed_at >= first_bar_start_at", name="symbol_timestamps_valid"),
        sa.CheckConstraint("raw_content_sha256 ~ '^[a-f0-9]{64}$'", name="symbol_raw_sha256_format"),
        sa.CheckConstraint("normalized_content_sha256 ~ '^[a-f0-9]{64}$'", name="symbol_norm_sha256_format"),
        sa.UniqueConstraint("dataset_id", "symbol", name="uq_dataset_symbol"),
        sa.UniqueConstraint("dataset_id", "stream_ordinal", name="uq_dataset_stream_ordinal"),
    )
    op.create_index("idx_dataset_symbols_inst", "historical_market_dataset_symbols", ["instrument_id"])
    op.execute("""
    CREATE OR REPLACE FUNCTION check_market_dataset_symbol_integrity() RETURNS TRIGGER AS $$
    DECLARE v_symbol VARCHAR(32); v_raw_hash VARCHAR(64); v_raw_role VARCHAR(32); v_norm_hash VARCHAR(64); v_norm_role VARCHAR(32);
    BEGIN
      SELECT symbol INTO v_symbol FROM instruments WHERE instrument_id = NEW.instrument_id;
      IF NOT FOUND THEN RAISE EXCEPTION 'Instrument % does not exist', NEW.instrument_id; END IF;
      IF v_symbol IS DISTINCT FROM NEW.symbol THEN RAISE EXCEPTION 'Symbol mismatch: % versus canonical %', NEW.symbol, v_symbol; END IF;
      SELECT content_sha256, artifact_role INTO v_raw_hash, v_raw_role FROM historical_market_artifacts WHERE artifact_id = NEW.raw_artifact_id;
      IF NOT FOUND THEN RAISE EXCEPTION 'Raw artifact % does not exist', NEW.raw_artifact_id; END IF;
      IF v_raw_role IS DISTINCT FROM 'RAW_PROVIDER_PAYLOAD' OR v_raw_hash IS DISTINCT FROM NEW.raw_content_sha256 THEN RAISE EXCEPTION 'Raw artifact lineage mismatch'; END IF;
      SELECT content_sha256, artifact_role INTO v_norm_hash, v_norm_role FROM historical_market_artifacts WHERE artifact_id = NEW.normalized_artifact_id;
      IF NOT FOUND THEN RAISE EXCEPTION 'Normalized artifact % does not exist', NEW.normalized_artifact_id; END IF;
      IF v_norm_role IS DISTINCT FROM 'NORMALIZED_RESEARCH_STREAM' OR v_norm_hash IS DISTINCT FROM NEW.normalized_content_sha256 THEN RAISE EXCEPTION 'Normalized artifact lineage mismatch'; END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER trg_check_market_dataset_symbol_integrity BEFORE INSERT ON historical_market_dataset_symbols FOR EACH ROW EXECUTE FUNCTION check_market_dataset_symbol_integrity();
    CREATE TRIGGER trg_historical_market_artifacts_immutable BEFORE UPDATE OR DELETE ON historical_market_artifacts FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();
    CREATE TRIGGER trg_historical_market_datasets_immutable BEFORE UPDATE OR DELETE ON historical_market_datasets FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();
    CREATE TRIGGER trg_historical_market_dataset_symbols_immutable BEFORE UPDATE OR DELETE ON historical_market_dataset_symbols FOR EACH ROW EXECUTE FUNCTION reject_intelligence_fact_mutation();
    """)
    op.execute("""
    DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='kairo_runtime') THEN
      REVOKE ALL ON historical_market_artifacts, historical_market_datasets, historical_market_dataset_symbols FROM kairo_runtime;
      GRANT SELECT, INSERT ON historical_market_artifacts, historical_market_datasets, historical_market_dataset_symbols TO kairo_runtime;
    END IF; END $$;
    """)


def downgrade() -> None:
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT 1 FROM historical_market_dataset_symbols) OR EXISTS (SELECT 1 FROM historical_market_datasets) OR EXISTS (SELECT 1 FROM historical_market_artifacts) THEN RAISE EXCEPTION 'Downgrade failed closed: immutable historical market data authority records exist'; END IF; END $$;""")
    op.execute("DROP TRIGGER IF EXISTS trg_historical_market_dataset_symbols_immutable ON historical_market_dataset_symbols")
    op.execute("DROP TRIGGER IF EXISTS trg_historical_market_datasets_immutable ON historical_market_datasets")
    op.execute("DROP TRIGGER IF EXISTS trg_historical_market_artifacts_immutable ON historical_market_artifacts")
    op.execute("DROP TRIGGER IF EXISTS trg_check_market_dataset_symbol_integrity ON historical_market_dataset_symbols")
    op.execute("DROP FUNCTION IF EXISTS check_market_dataset_symbol_integrity()")
    op.drop_table("historical_market_dataset_symbols")
    op.drop_table("historical_market_datasets")
    op.drop_table("historical_market_artifacts")
