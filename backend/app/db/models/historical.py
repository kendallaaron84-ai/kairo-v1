from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class HistoricalMarketArtifact(Base):
    __tablename__ = "historical_market_artifacts"
    __table_args__ = (
        CheckConstraint("artifact_role IN ('RAW_PROVIDER_PAYLOAD','NORMALIZED_RESEARCH_STREAM')", name="market_artifact_role"),
        CheckConstraint("byte_size > 0", name="market_artifact_size_pos"),
        CheckConstraint("content_sha256 ~ '^[a-f0-9]{64}$'", name="market_artifact_sha256_format"),
        Index("idx_market_artifacts_hash", "content_sha256"),
    )
    artifact_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    artifact_role: Mapped[str] = mapped_column(String(32), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_uri: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HistoricalMarketDataset(Base):
    __tablename__ = "historical_market_datasets"
    __table_args__ = (
        CheckConstraint("bar_interval_seconds > 0", name="dataset_bar_interval_pos"),
        CheckConstraint("source_timestamp_convention IN ('INTERVAL_BEGIN','INTERVAL_END','TICK_ARRIVAL')", name="dataset_timestamp_convention"),
        CheckConstraint("liquidity_fidelity_tier IN ('TIER_1_QUOTE_DEPTH','TIER_2_TRADE_HISTORY','TIER_3_BAR_ONLY')", name="dataset_fidelity_tier"),
        CheckConstraint("price_adjustment_mode IN ('RAW_UNADJUSTED','SPLIT_ADJUSTED_SERIES','SPLIT_AND_DIVIDEND_ADJUSTED')", name="dataset_price_adj_mode"),
        CheckConstraint("(price_adjustment_mode = 'RAW_UNADJUSTED' AND adjustment_policy_version IS NULL) OR (price_adjustment_mode <> 'RAW_UNADJUSTED' AND adjustment_policy_version IS NOT NULL)", name="dataset_adj_version_parity"),
        CheckConstraint("coverage_end >= coverage_start", name="dataset_coverage_valid"),
        CheckConstraint("dataset_manifest_sha256 ~ '^[a-f0-9]{64}$'", name="dataset_manifest_sha256_format"),
        Index("idx_datasets_provider_cov", "provider_name", "coverage_start", "coverage_end"),
    )
    dataset_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    dataset_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    bar_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    source_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    calendar_name: Mapped[str] = mapped_column(String(64), nullable=False)
    calendar_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_timestamp_convention: Mapped[str] = mapped_column(String(32), nullable=False)
    liquidity_fidelity_tier: Mapped[str] = mapped_column(String(32), nullable=False)
    price_adjustment_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    adjustment_policy_version: Mapped[str | None] = mapped_column(String(64))
    normalization_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    coverage_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    coverage_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dataset_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HistoricalMarketDatasetSymbol(Base):
    __tablename__ = "historical_market_dataset_symbols"
    __table_args__ = (
        CheckConstraint("stream_role IN ('UNDERLYING_SIGNAL_BARS','OPTION_CHAIN_QUOTES','CONTEXT_MACRO_SERIES')", name="symbol_stream_role"),
        CheckConstraint("stream_ordinal >= 0", name="symbol_stream_ordinal_pos"),
        CheckConstraint("bar_count > 0", name="symbol_bar_count_pos"),
        CheckConstraint("last_bar_completed_at >= first_bar_start_at", name="symbol_timestamps_valid"),
        CheckConstraint("raw_content_sha256 ~ '^[a-f0-9]{64}$'", name="symbol_raw_sha256_format"),
        CheckConstraint("normalized_content_sha256 ~ '^[a-f0-9]{64}$'", name="symbol_norm_sha256_format"),
        UniqueConstraint(
            "dataset_id", "symbol", "stream_role",
            name="uq_dataset_symbol_stream_role",
        ),
        UniqueConstraint("dataset_id", "stream_ordinal", name="uq_dataset_stream_ordinal"),
        Index("idx_dataset_symbols_inst", "instrument_id"),
    )
    symbol_entry_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    dataset_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("historical_market_datasets.dataset_id"), nullable=False)
    instrument_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("instruments.instrument_id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    stream_role: Mapped[str] = mapped_column(String(32), nullable=False)
    stream_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_artifact_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("historical_market_artifacts.artifact_id"), nullable=False)
    raw_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_artifact_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("historical_market_artifacts.artifact_id"), nullable=False)
    normalized_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    bar_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    first_bar_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_bar_completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
