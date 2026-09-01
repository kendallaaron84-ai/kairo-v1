import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.db.models.configuration import Instrument
from app.db.models.historical import HistoricalMarketArtifact, HistoricalMarketDataset, HistoricalMarketDatasetSymbol
from engine.strategy.option_resolver import OptionContractCandidate, is_legacy_eligible
from engine.validation.adapter import HistoricalReplayAdapter
from engine.validation.feed_loader import DataNormalizer, HistoricalDatasetRegistry
from engine.validation.models import SourceTimestampConvention, StreamRole
from engine.validation.session_calendar import SessionCalendarResolver

NOW = datetime(2026, 3, 9, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def instruments(db_session: Session) -> tuple[Instrument, Instrument]:
    underlying = Instrument(instrument_id=uuid4(), symbol="TQQQ", asset_class="EQUITY", currency="USD", effective_from=NOW)
    option = Instrument(
        instrument_id=uuid4(), symbol="TQQQ260309C00050000", asset_class="OPTION", currency="USD",
        underlying_symbol="TQQQ", contract_symbol="TQQQ260309C00050000", expiration_date=date(2026, 3, 9),
        strike_price=Decimal("50"), option_right="CALL", contract_multiplier=Decimal("100"),
        listing_type="STANDARD", effective_from=NOW,
    )
    db_session.add_all([underlying, option]); db_session.flush()
    return underlying, option


def _bars() -> bytes:
    return b"timestamp,open,high,low,close,volume\n2026-03-09T09:30:00,50,51,49,50.5,100\n"


def _option_payload(underlying: Instrument, option: Instrument, **overrides) -> bytes:
    contract = {
        "instrument_id": str(option.instrument_id), "contract_symbol": option.contract_symbol,
        "expiration_date": option.expiration_date.isoformat(), "strike_price": "50", "option_right": "CALL",
        "contract_multiplier": "100", "listing_type": "STANDARD", "bid_price": "0.47", "ask_price": "0.50",
        "bid_size": "10", "ask_size": "12", "volume": None, "open_interest": None,
    }
    contract.update(overrides)
    return json.dumps({"snapshots": [{"underlying_instrument_id": str(underlying.instrument_id), "underlying_symbol": underlying.symbol, "completed_at": "2026-03-09T14:31:00+00:00", "contracts": [contract]}]}).encode()


def _artifact(role: str, content: bytes, suffix: str = "json") -> HistoricalMarketArtifact:
    digest = hashlib.sha256(content).hexdigest()
    return HistoricalMarketArtifact(artifact_id=uuid4(), artifact_role=role, content_sha256=digest, mime_type=f"application/{suffix}", byte_size=len(content), storage_uri=f"file:///evidence/{digest}.{suffix}", created_at=NOW)


def _dataset() -> HistoricalMarketDataset:
    return HistoricalMarketDataset(
        dataset_id=uuid4(), dataset_name="STEP1-FIXTURE", provider_name="FIXTURE", bar_interval_seconds=60,
        source_timezone="America/New_York", calendar_name="XNYS", calendar_version="CAL-US-EQUITIES-2026-v1",
        source_timestamp_convention="INTERVAL_BEGIN", liquidity_fidelity_tier="TIER_1_QUOTE_DEPTH",
        price_adjustment_mode="RAW_UNADJUSTED", adjustment_policy_version=None,
        normalization_policy_version="NORM-BAR-UTC-CAUSAL-v1", coverage_start=NOW, coverage_end=NOW,
        dataset_manifest_sha256=hashlib.sha256(str(uuid4()).encode()).hexdigest(), ingested_at=NOW,
    )


def _symbol(dataset, instrument, raw, normalized, *, ordinal=0, symbol=None):
    return HistoricalMarketDatasetSymbol(
        symbol_entry_id=uuid4(), dataset_id=dataset.dataset_id, instrument_id=instrument.instrument_id,
        symbol=symbol or instrument.symbol, stream_role="UNDERLYING_SIGNAL_BARS", stream_ordinal=ordinal,
        raw_artifact_id=raw.artifact_id, raw_content_sha256=raw.content_sha256,
        normalized_artifact_id=normalized.artifact_id, normalized_content_sha256=normalized.content_sha256,
        bar_count=1, first_bar_start_at=NOW, last_bar_completed_at=NOW,
    )


def test_raw_market_artifact_persists_with_exact_sha256_digest(db_session, tmp_path):
    content = b"provider bytes\r\nexact"
    row = HistoricalDatasetRegistry(db_session, tmp_path).persist_artifact(content, role="RAW_PROVIDER_PAYLOAD", mime_type="application/json", created_at=NOW)
    assert row.content_sha256 == hashlib.sha256(content).hexdigest()


def test_dataset_symbols_enforces_non_null_canonical_instrument_id(db_session, instruments):
    raw, norm, ds = _artifact("RAW_PROVIDER_PAYLOAD", b"r"), _artifact("NORMALIZED_RESEARCH_STREAM", b"n"), _dataset()
    db_session.add_all([raw, norm, ds]); db_session.flush()
    row = _symbol(ds, instruments[0], raw, norm); row.instrument_id = None
    with pytest.raises(DBAPIError, match="does not exist"), db_session.begin_nested(): db_session.add(row); db_session.flush()


def test_database_rejects_symbol_entry_when_symbol_does_not_match_instrument(db_session, instruments):
    raw, norm, ds = _artifact("RAW_PROVIDER_PAYLOAD", b"r2"), _artifact("NORMALIZED_RESEARCH_STREAM", b"n2"), _dataset()
    db_session.add_all([raw, norm, ds]); db_session.flush()
    with pytest.raises(DBAPIError, match="Symbol mismatch"), db_session.begin_nested(): db_session.add(_symbol(ds, instruments[0], raw, norm, symbol="SQQQ")); db_session.flush()


def test_database_rejects_symbol_entry_when_artifact_content_hash_mismatches(db_session, instruments):
    raw, norm, ds = _artifact("RAW_PROVIDER_PAYLOAD", b"r3"), _artifact("NORMALIZED_RESEARCH_STREAM", b"n3"), _dataset()
    db_session.add_all([raw, norm, ds]); db_session.flush(); row = _symbol(ds, instruments[0], raw, norm); row.raw_content_sha256 = "0" * 64
    with pytest.raises(DBAPIError, match="lineage mismatch"), db_session.begin_nested(): db_session.add(row); db_session.flush()


def test_database_enforces_unique_stream_ordinals_per_dataset(db_session, instruments):
    second = Instrument(instrument_id=uuid4(), symbol="SQQQ", asset_class="EQUITY", currency="USD", effective_from=NOW)
    raw, norm, ds = _artifact("RAW_PROVIDER_PAYLOAD", b"r4"), _artifact("NORMALIZED_RESEARCH_STREAM", b"n4"), _dataset()
    db_session.add_all([second, raw, norm, ds]); db_session.flush(); db_session.add(_symbol(ds, instruments[0], raw, norm)); db_session.flush()
    with pytest.raises(IntegrityError), db_session.begin_nested(): db_session.add(_symbol(ds, second, raw, norm)); db_session.flush()


def test_data_normalizer_calculates_canonical_completed_at_without_lookahead(db_session, instruments):
    bars = DataNormalizer(db_session).normalize_bars(_bars(), instrument_id=instruments[0].instrument_id, symbol="TQQQ", source_timezone="America/New_York", timestamp_convention=SourceTimestampConvention.INTERVAL_BEGIN, bar_interval_seconds=60)
    assert bars[0].interval_start_at.isoformat() == "2026-03-09T13:30:00+00:00"
    assert bars[0].completed_at.isoformat() == "2026-03-09T13:31:00+00:00"


def test_session_calendar_filters_bars_outside_regular_trading_hours(db_session, instruments):
    raw = b"timestamp,open,high,low,close,volume\n2026-03-09T09:29:00,50,51,49,50,1\n2026-03-09T09:30:00,50,51,49,50,1\n"
    bars = DataNormalizer(db_session).normalize_bars(raw, instrument_id=instruments[0].instrument_id, symbol="TQQQ", source_timezone="America/New_York", timestamp_convention=SourceTimestampConvention.INTERVAL_BEGIN, bar_interval_seconds=60)
    assert len(bars) == 1


def test_session_calendar_handles_dst_shifts_and_early_close_days():
    calendar = SessionCalendarResolver()
    assert calendar.session_bounds(date(2026, 3, 6))[0].astimezone(timezone.utc).hour == 14
    assert calendar.session_bounds(date(2026, 3, 9))[0].astimezone(timezone.utc).hour == 13
    assert calendar.session_bounds(date(2026, 11, 27))[1].hour == 13


def test_dataset_enforces_raw_unadjusted_mode_without_adjustment_policy(db_session):
    row = _dataset(); row.adjustment_policy_version = "fabricated"
    with pytest.raises(IntegrityError), db_session.begin_nested(): db_session.add(row); db_session.flush()


def test_underlying_signal_bars_cannot_be_represented_as_option_chains(db_session, instruments):
    snapshots = DataNormalizer(db_session).normalize_option_chains(_option_payload(*instruments))
    with pytest.raises(ValueError, match="OPTION_CHAIN_QUOTES"): HistoricalReplayAdapter("dataset-x").option_chains(snapshots, stream_role=StreamRole.UNDERLYING_SIGNAL_BARS)


def test_option_chain_contracts_resolve_to_canonical_contract_instrument_ids(db_session, instruments):
    snapshot = DataNormalizer(db_session).normalize_option_chains(_option_payload(*instruments))[0]
    assert snapshot.contracts[0].contract_instrument_id == instruments[1].instrument_id


def test_option_chain_event_preserves_contract_execution_evidence(db_session, instruments):
    snapshot = DataNormalizer(db_session).normalize_option_chains(_option_payload(*instruments))[0]
    event = HistoricalReplayAdapter("dataset-x").option_chains((snapshot,))[0]
    assert (event.candidates[0].bid, event.candidates[0].ask, event.candidates[0].bid_size, event.candidates[0].ask_size) == (Decimal("0.47"), Decimal("0.50"), Decimal("10"), Decimal("12"))


def test_option_chain_candidate_preserves_nullable_volume_and_open_interest(db_session, instruments):
    event = HistoricalReplayAdapter("dataset-x").option_chains(DataNormalizer(db_session).normalize_option_chains(_option_payload(*instruments)))[0]
    assert event.candidates[0].volume is None and event.candidates[0].open_interest is None


def test_liquidity_filter_fails_closed_when_volume_and_open_interest_are_none(db_session, instruments):
    event = HistoricalReplayAdapter("dataset-x").option_chains(DataNormalizer(db_session).normalize_option_chains(_option_payload(*instruments)))[0]
    assert not is_legacy_eligible(event.candidates[0].resolver_candidate())


def test_data_normalizer_validates_candidates_using_canonical_candidate_validator(db_session, instruments, monkeypatch):
    import engine.validation.feed_loader as loader
    called = []
    original = loader.validate_candidate_identity
    def spy(candidate, canonical): called.append(candidate.instrument_id); return original(candidate, canonical)
    monkeypatch.setattr(loader, "validate_candidate_identity", spy)
    DataNormalizer(db_session).normalize_option_chains(_option_payload(*instruments))
    assert called == [instruments[1].instrument_id]


def test_immutability_triggers_reject_update_and_delete_across_all_three_tables(db_session, instruments):
    raw, norm, ds = _artifact("RAW_PROVIDER_PAYLOAD", b"r5"), _artifact("NORMALIZED_RESEARCH_STREAM", b"n5"), _dataset()
    db_session.add_all([raw, norm, ds]); db_session.flush(); symbol = _symbol(ds, instruments[0], raw, norm); db_session.add(symbol); db_session.flush()
    for statement in (text("UPDATE historical_market_artifacts SET mime_type='x' WHERE artifact_id=:id").bindparams(id=raw.artifact_id), text("DELETE FROM historical_market_datasets WHERE dataset_id=:id").bindparams(id=ds.dataset_id), text("DELETE FROM historical_market_dataset_symbols WHERE symbol_entry_id=:id").bindparams(id=symbol.symbol_entry_id)):
        with pytest.raises(DBAPIError, match="immutable"), db_session.begin_nested(): db_session.execute(statement)


def test_migration_0023_downgrade_fails_closed_when_any_table_contains_data(migrated_database):
    admin_url, _ = migrated_database; root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini")); config.set_main_option("script_location", str(root / "alembic")); engine = create_engine(admin_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO historical_market_artifacts VALUES (:id,'RAW_PROVIDER_PAYLOAD',:h,'application/json',1,'file:///x',:now)"), {"id": uuid4(), "h": hashlib.sha256(b"x").hexdigest(), "now": NOW})
        with pytest.raises(Exception, match="Downgrade failed closed"): command.downgrade(config, "0022")
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE historical_market_artifacts DISABLE TRIGGER trg_historical_market_artifacts_immutable")); connection.execute(text("DELETE FROM historical_market_artifacts"))
        command.downgrade(config, "0022"); assert "historical_market_artifacts" not in inspect(engine).get_table_names(); command.upgrade(config, "head")
    finally: engine.dispose()


def test_normalized_research_stream_is_byte_exact_and_reproducible(db_session, instruments):
    normalizer = DataNormalizer(db_session); values = normalizer.normalize_bars(_bars(), instrument_id=instruments[0].instrument_id, symbol="TQQQ", source_timezone="America/New_York", timestamp_convention=SourceTimestampConvention.INTERVAL_BEGIN, bar_interval_seconds=60)
    assert normalizer.normalized_bytes(values) == normalizer.normalized_bytes(values)
    assert hashlib.sha256(normalizer.normalized_bytes(values)).hexdigest() == hashlib.sha256(normalizer.normalized_bytes(values)).hexdigest()


def test_ingestion_adapter_emits_canonical_research_replay_input(db_session, instruments):
    bars = DataNormalizer(db_session).normalize_bars(_bars(), instrument_id=instruments[0].instrument_id, symbol="TQQQ", source_timezone="America/New_York", timestamp_convention=SourceTimestampConvention.INTERVAL_BEGIN, bar_interval_seconds=60)
    stream = HistoricalReplayAdapter("dataset-manifest-x").bars(bars)
    assert stream.provider.lineage.replay_mode.value == "RESEARCH_REPLAY_MODE"
    assert stream.provider.lineage.exact_prototype_replay is False
    assert stream.events[0].timestamp == bars[0].completed_at
