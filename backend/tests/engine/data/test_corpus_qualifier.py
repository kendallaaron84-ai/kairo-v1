import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.db.models.configuration import Instrument
from app.db.models.historical import (
    HistoricalMarketArtifact,
    HistoricalMarketDataset,
    HistoricalMarketDatasetSymbol,
)
from app.domain.enums import OptionRight
from engine.data.corpus_qualifier import (
    CorpusQualificationEngine,
    CorpusQualificationInput,
    PilotDecisionPoint,
    QualificationStatus,
)
from engine.data.provider_adapter import ThetaDataProviderAdapter
from engine.validation.feed_loader import DataNormalizer, HistoricalDatasetRegistry
from engine.validation.models import (
    CanonicalMarketBar,
    CanonicalOptionChainSnapshot,
    CanonicalOptionContractQuote,
    StreamRole,
)

NOW = datetime(2024, 1, 2, 15, 31, tzinfo=timezone.utc)
SHA_A = hashlib.sha256(b"raw-a").hexdigest()
SHA_B = hashlib.sha256(b"normalized-b").hexdigest()


@pytest.fixture
def underlying(db_session: Session) -> Instrument:
    row = Instrument(
        instrument_id=uuid4(),
        symbol=f"T{uuid4().hex[:7].upper()}",
        asset_class="EQUITY",
        currency="USD",
        effective_from=NOW,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _bar(instrument: Instrument, completed_at: datetime = NOW) -> CanonicalMarketBar:
    return CanonicalMarketBar(
        instrument_id=instrument.instrument_id,
        symbol=instrument.symbol,
        interval_start_at=completed_at - timedelta(minutes=1),
        completed_at=completed_at,
        open=Decimal("50"),
        high=Decimal("51"),
        low=Decimal("49"),
        close=Decimal("50.5"),
        volume=None,
    )


def _quote(
    *,
    underlying: Instrument,
    instrument_id: UUID | None = None,
    strike: Decimal = Decimal("50"),
    expiration: date = date(2024, 1, 2),
    right: OptionRight = OptionRight.CALL,
    bid_size: Decimal = Decimal("10"),
    ask_size: Decimal = Decimal("12"),
    volume: int | None = None,
    open_interest: int | None = None,
) -> CanonicalOptionContractQuote:
    suffix = "C" if right is OptionRight.CALL else "P"
    symbol = f"{underlying.symbol}240102{suffix}{int(strike * 1000):08d}"
    return CanonicalOptionContractQuote(
        contract_instrument_id=instrument_id or uuid4(),
        underlying_instrument_id=underlying.instrument_id,
        underlying_symbol=underlying.symbol,
        canonical_contract_symbol=symbol,
        expiration_date=expiration,
        strike_price=strike,
        option_right=right,
        contract_multiplier=Decimal("100"),
        listing_type="STANDARD",
        bid_price=Decimal("0.47"),
        ask_price=Decimal("0.50"),
        bid_size=bid_size,
        ask_size=ask_size,
        volume=volume,
        open_interest=open_interest,
        liquidity_verifiable=volume is not None and open_interest is not None,
    )


def _input(
    underlying: Instrument,
    *,
    bars=(),
    snapshots=(),
    decisions=(),
) -> CorpusQualificationInput:
    return CorpusQualificationInput(
        provider_code="THETA_DATA",
        start_session=date(2024, 1, 2),
        end_session=date(2024, 1, 2),
        symbols=(underlying.symbol,),
        bars=tuple(bars),
        option_snapshots=tuple(snapshots),
        decision_points=tuple(decisions),
        raw_artifact_sha256s=(SHA_A,),
        normalized_dataset_manifest_sha256=SHA_B,
    )


def _artifact(role: str, content: bytes) -> HistoricalMarketArtifact:
    digest = hashlib.sha256(content).hexdigest()
    return HistoricalMarketArtifact(
        artifact_id=uuid4(),
        artifact_role=role,
        content_sha256=digest,
        mime_type="application/json",
        byte_size=len(content),
        storage_uri=f"file:///evidence/{digest}.json",
        created_at=NOW,
    )


def _dataset() -> HistoricalMarketDataset:
    return HistoricalMarketDataset(
        dataset_id=uuid4(),
        dataset_name=f"STAGE1-{uuid4()}",
        provider_name="FIXTURE",
        bar_interval_seconds=60,
        source_timezone="America/New_York",
        calendar_name="XNYS",
        calendar_version="CAL-US-EQUITIES-2026-v1",
        source_timestamp_convention="INTERVAL_BEGIN",
        liquidity_fidelity_tier="TIER_1_QUOTE_DEPTH",
        price_adjustment_mode="RAW_UNADJUSTED",
        adjustment_policy_version=None,
        normalization_policy_version="NORM-PILOT-CORPUS-v1",
        coverage_start=NOW,
        coverage_end=NOW,
        dataset_manifest_sha256=hashlib.sha256(uuid4().bytes).hexdigest(),
        ingested_at=NOW,
    )


def _symbol_row(dataset, instrument, raw, normalized, role, ordinal):
    return HistoricalMarketDatasetSymbol(
        symbol_entry_id=uuid4(),
        dataset_id=dataset.dataset_id,
        instrument_id=instrument.instrument_id,
        symbol=instrument.symbol,
        stream_role=role,
        stream_ordinal=ordinal,
        raw_artifact_id=raw.artifact_id,
        raw_content_sha256=raw.content_sha256,
        normalized_artifact_id=normalized.artifact_id,
        normalized_content_sha256=normalized.content_sha256,
        bar_count=1,
        first_bar_start_at=NOW,
        last_bar_completed_at=NOW,
    )


def test_migration_0027_allows_coexisting_symbol_streams_via_stream_role(
    db_session: Session, underlying: Instrument
):
    raw = _artifact("RAW_PROVIDER_PAYLOAD", b"coexist-raw")
    normalized = _artifact("NORMALIZED_RESEARCH_STREAM", b"coexist-normalized")
    dataset = _dataset()
    db_session.add_all([raw, normalized, dataset])
    db_session.flush()
    db_session.add_all(
        [
            _symbol_row(dataset, underlying, raw, normalized, "UNDERLYING_SIGNAL_BARS", 0),
            _symbol_row(dataset, underlying, raw, normalized, "OPTION_CHAIN_QUOTES", 1),
        ]
    )
    db_session.flush()
    assert len(
        db_session.query(HistoricalMarketDatasetSymbol)
        .filter_by(dataset_id=dataset.dataset_id, symbol=underlying.symbol)
        .all()
    ) == 2


def test_migration_0027_prohibits_duplicate_symbol_role_streams(
    db_session: Session, underlying: Instrument
):
    raw = _artifact("RAW_PROVIDER_PAYLOAD", b"duplicate-raw")
    normalized = _artifact("NORMALIZED_RESEARCH_STREAM", b"duplicate-normalized")
    dataset = _dataset()
    db_session.add_all([raw, normalized, dataset])
    db_session.flush()
    db_session.add(
        _symbol_row(dataset, underlying, raw, normalized, "UNDERLYING_SIGNAL_BARS", 0)
    )
    db_session.flush()
    with pytest.raises(IntegrityError), db_session.begin_nested():
        db_session.add(
            _symbol_row(dataset, underlying, raw, normalized, "UNDERLYING_SIGNAL_BARS", 1)
        )
        db_session.flush()


def _alembic_config() -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    return config


def _persistent_authority_rows(connection, *, two_roles: bool) -> tuple[UUID, UUID, UUID, UUID]:
    instrument_id, raw_id, normalized_id, dataset_id = uuid4(), uuid4(), uuid4(), uuid4()
    symbol = f"M{uuid4().hex[:7].upper()}"
    raw_hash = hashlib.sha256(raw_id.bytes).hexdigest()
    normalized_hash = hashlib.sha256(normalized_id.bytes).hexdigest()
    connection.execute(
        text("INSERT INTO instruments (instrument_id,symbol,asset_class,currency,effective_from) VALUES (:id,:symbol,'EQUITY','USD',:now)"),
        {"id": instrument_id, "symbol": symbol, "now": NOW},
    )
    connection.execute(
        text("INSERT INTO historical_market_artifacts VALUES (:id,'RAW_PROVIDER_PAYLOAD',:hash,'application/json',1,:uri,:now)"),
        {"id": raw_id, "hash": raw_hash, "uri": f"file:///{raw_hash}", "now": NOW},
    )
    connection.execute(
        text("INSERT INTO historical_market_artifacts VALUES (:id,'NORMALIZED_RESEARCH_STREAM',:hash,'application/json',1,:uri,:now)"),
        {"id": normalized_id, "hash": normalized_hash, "uri": f"file:///{normalized_hash}", "now": NOW},
    )
    connection.execute(
        text("""
        INSERT INTO historical_market_datasets VALUES
        (:id,:name,'FIXTURE',60,'America/New_York','XNYS','CAL-US-EQUITIES-2026-v1',
         'INTERVAL_BEGIN','TIER_1_QUOTE_DEPTH','RAW_UNADJUSTED',NULL,
         'NORM-PILOT-CORPUS-v1',:now,:now,:hash,:now)
        """),
        {"id": dataset_id, "name": f"MIGRATION-{dataset_id}", "now": NOW, "hash": hashlib.sha256(dataset_id.bytes).hexdigest()},
    )
    roles = ("UNDERLYING_SIGNAL_BARS", "OPTION_CHAIN_QUOTES") if two_roles else ("UNDERLYING_SIGNAL_BARS",)
    for ordinal, role in enumerate(roles):
        connection.execute(
            text("""
            INSERT INTO historical_market_dataset_symbols VALUES
            (:entry,:dataset,:instrument,:symbol,:role,:ordinal,:raw,:raw_hash,:norm,:norm_hash,1,:now,:now)
            """),
            {
                "entry": uuid4(), "dataset": dataset_id, "instrument": instrument_id,
                "symbol": symbol, "role": role, "ordinal": ordinal, "raw": raw_id,
                "raw_hash": raw_hash, "norm": normalized_id, "norm_hash": normalized_hash,
                "now": NOW,
            },
        )
    return instrument_id, raw_id, normalized_id, dataset_id


def _cleanup_persistent_rows(engine, ids) -> None:
    instrument_id, raw_id, normalized_id, dataset_id = ids
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE historical_market_dataset_symbols DISABLE TRIGGER trg_historical_market_dataset_symbols_immutable"))
        connection.execute(text("ALTER TABLE historical_market_datasets DISABLE TRIGGER trg_historical_market_datasets_immutable"))
        connection.execute(text("ALTER TABLE historical_market_artifacts DISABLE TRIGGER trg_historical_market_artifacts_immutable"))
        connection.execute(text("DELETE FROM historical_market_dataset_symbols WHERE dataset_id=:id"), {"id": dataset_id})
        connection.execute(text("DELETE FROM historical_market_datasets WHERE dataset_id=:id"), {"id": dataset_id})
        connection.execute(text("DELETE FROM historical_market_artifacts WHERE artifact_id IN (:raw,:norm)"), {"raw": raw_id, "norm": normalized_id})
        connection.execute(text("DELETE FROM instruments WHERE instrument_id=:id"), {"id": instrument_id})
        connection.execute(text("ALTER TABLE historical_market_dataset_symbols ENABLE TRIGGER trg_historical_market_dataset_symbols_immutable"))
        connection.execute(text("ALTER TABLE historical_market_datasets ENABLE TRIGGER trg_historical_market_datasets_immutable"))
        connection.execute(text("ALTER TABLE historical_market_artifacts ENABLE TRIGGER trg_historical_market_artifacts_immutable"))


def test_migration_0027_migrates_existing_rows_without_reinterpretation(migrated_database):
    engine = create_engine(migrated_database[0])
    config = _alembic_config()
    ids = None
    command.downgrade(config, "0026")
    try:
        with engine.begin() as connection:
            ids = _persistent_authority_rows(connection, two_roles=False)
            before = connection.execute(
                text("SELECT instrument_id,symbol,stream_role,stream_ordinal FROM historical_market_dataset_symbols WHERE dataset_id=:id"),
                {"id": ids[3]},
            ).one()
        command.upgrade(config, "0027")
        with engine.begin() as connection:
            after = connection.execute(
                text("SELECT instrument_id,symbol,stream_role,stream_ordinal FROM historical_market_dataset_symbols WHERE dataset_id=:id"),
                {"id": ids[3]},
            ).one()
        assert after == before
        command.downgrade(config, "0026")
        command.upgrade(config, "0027")
        assert "historical_market_dataset_symbols" in inspect(engine).get_table_names()
    finally:
        if inspect(engine).has_table("historical_market_dataset_symbols"):
            with engine.connect() as connection:
                version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            if version == "0026":
                command.upgrade(config, "0027")
            if ids is not None:
                _cleanup_persistent_rows(engine, ids)
        engine.dispose()


def test_migration_0027_downgrade_fails_closed_for_multi_role_symbol_facts(migrated_database):
    engine = create_engine(migrated_database[0])
    config = _alembic_config()
    ids = None
    try:
        with engine.begin() as connection:
            ids = _persistent_authority_rows(connection, two_roles=True)
        with pytest.raises(Exception, match="Downgrade failed closed"):
            command.downgrade(config, "0026")
        with engine.begin() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0027"
    finally:
        if ids is not None:
            _cleanup_persistent_rows(engine, ids)
        engine.dispose()


def test_qualifier_evaluates_three_state_rubric_accurately(db_session: Session):
    engine = CorpusQualificationEngine(db_session)
    assert engine._threshold(Decimal("99.80"), Decimal("99.80"), Decimal("99.00")) is QualificationStatus.PASS
    assert engine._threshold(Decimal("99.79"), Decimal("99.80"), Decimal("99.00")) is QualificationStatus.REVIEW
    assert engine._threshold(Decimal("98.99"), Decimal("99.80"), Decimal("99.00")) is QualificationStatus.FAIL


def test_qualifier_flags_causal_or_ordering_violations_as_hard_fail(
    db_session: Session, underlying: Instrument
):
    values = (_bar(underlying, NOW), _bar(underlying, NOW - timedelta(minutes=1)))
    manifest = CorpusQualificationEngine(db_session).qualify(_input(underlying, bars=values))
    assert manifest.metrics.causal_timestamp_violations_count == 1
    assert manifest.metrics.causal_status is QualificationStatus.FAIL
    assert manifest.overall_qualification_verdict is QualificationStatus.FAIL


def test_qualifier_requires_full_strike_neighborhood_not_cherrypicked_contract(
    db_session: Session, underlying: Instrument
):
    decision = PilotDecisionPoint(
        underlying_instrument_id=underlying.instrument_id,
        symbol=underlying.symbol,
        signal_at=NOW,
        underlying_spot=Decimal("50"),
    )
    snapshot = CanonicalOptionChainSnapshot(
        underlying_instrument_id=underlying.instrument_id,
        underlying_symbol=underlying.symbol,
        canonical_completed_at=NOW,
        contracts=(_quote(underlying=underlying),),
    )
    evidence = _input(underlying, snapshots=(snapshot,), decisions=(decision,))
    manifest = CorpusQualificationEngine(db_session).qualify(evidence)
    assert manifest.metrics.decision_point_complete_evidence_count == 0
    assert manifest.metrics.decision_evidence_status is QualificationStatus.FAIL


def test_qualifier_preserves_nullable_volume_and_open_interest(
    db_session: Session, underlying: Instrument
):
    option = Instrument(
        instrument_id=uuid4(), symbol=f"{underlying.symbol}240102C00050000",
        asset_class="OPTION", currency="USD", underlying_symbol=underlying.symbol,
        contract_symbol=f"{underlying.symbol}240102C00050000", expiration_date=date(2024, 1, 2),
        strike_price=Decimal("50"), option_right="CALL", contract_multiplier=Decimal("100"),
        listing_type="STANDARD", effective_from=NOW,
    )
    db_session.add(option); db_session.flush()
    payload = json.dumps({"snapshots": [{
        "underlying_instrument_id": str(underlying.instrument_id), "underlying_symbol": underlying.symbol,
        "completed_at": NOW.isoformat(), "contracts": [{
            "instrument_id": str(option.instrument_id), "contract_symbol": option.contract_symbol,
            "expiration_date": "2024-01-02", "strike_price": "50", "option_right": "CALL",
            "contract_multiplier": "100", "listing_type": "STANDARD", "bid_price": "0.47",
            "ask_price": "0.50", "bid_size": "10", "ask_size": "12",
            "volume": None, "open_interest": None,
        }]
    }]}).encode()
    contract = DataNormalizer(db_session).normalize_option_chains(payload)[0].contracts[0]
    assert contract.volume is None and contract.open_interest is None


def test_qualifier_bounds_liquidity_fidelity_tier_based_on_quote_fields(
    db_session: Session, underlying: Instrument
):
    engine = CorpusQualificationEngine(db_session)
    def evidence(quote):
        snapshot = CanonicalOptionChainSnapshot(
            underlying_instrument_id=underlying.instrument_id,
            underlying_symbol=underlying.symbol,
            canonical_completed_at=NOW,
            contracts=(quote,),
        )
        return _input(underlying, snapshots=(snapshot,))
    assert engine._fidelity_tier(evidence(_quote(underlying=underlying))) == "TIER_1_QUOTE_DEPTH"
    assert engine._fidelity_tier(evidence(_quote(underlying=underlying, bid_size=Decimal("0"), ask_size=Decimal("0"), volume=1))) == "TIER_2_TRADE_HISTORY"
    assert engine._fidelity_tier(evidence(_quote(underlying=underlying, bid_size=Decimal("0"), ask_size=Decimal("0")))) == "TIER_3_BAR_ONLY"


def test_raw_artifacts_and_qualification_manifest_hashes_are_deterministic(
    db_session: Session, underlying: Instrument
):
    transport = lambda _kind, _params: b"exact-provider-response"
    adapter = ThetaDataProviderAdapter(transport)
    first = adapter.fetch_equity_minute_bars(symbol="TQQQ", start_session=date(2024, 1, 2), end_session=date(2024, 3, 28))
    second = adapter.fetch_equity_minute_bars(symbol="TQQQ", start_session=date(2024, 1, 2), end_session=date(2024, 3, 28))
    assert first.content_sha256 == second.content_sha256
    engine = CorpusQualificationEngine(db_session)
    evidence = _input(underlying, bars=(_bar(underlying),))
    one, two = engine.qualify(evidence), engine.qualify(evidence)
    assert one == two
    assert hashlib.sha256(one.canonical_bytes()).hexdigest() == hashlib.sha256(two.canonical_bytes()).hexdigest()


def test_pilot_ingestion_integrates_seamlessly_with_migration_0023_and_0027_authority(
    db_session: Session, underlying: Instrument, tmp_path
):
    bar = _bar(underlying)
    quote = _quote(underlying=underlying)
    snapshot = CanonicalOptionChainSnapshot(
        underlying_instrument_id=underlying.instrument_id,
        underlying_symbol=underlying.symbol,
        canonical_completed_at=NOW,
        contracts=(quote,),
    )
    registry = HistoricalDatasetRegistry(db_session, tmp_path)
    manifest = registry.register_dataset(
        dataset_name=f"PILOT-{uuid4()}", provider_name="THETA_DATA", bar_interval_seconds=60,
        source_timezone="America/New_York", source_timestamp_convention="INTERVAL_BEGIN",
        liquidity_fidelity_tier="TIER_1_QUOTE_DEPTH", price_adjustment_mode="RAW_UNADJUSTED",
        adjustment_policy_version=None, normalization_policy_version="NORM-PILOT-CORPUS-v1",
        ingested_at=NOW,
        streams=(
            {"instrument_id": underlying.instrument_id, "symbol": underlying.symbol,
             "stream_role": StreamRole.UNDERLYING_SIGNAL_BARS, "stream_ordinal": 0,
             "raw_bytes": b"raw-bars", "normalized_bytes": DataNormalizer.normalized_bytes((bar,)),
             "bar_count": 1, "first_bar_start_at": bar.interval_start_at,
             "last_bar_completed_at": bar.completed_at},
            {"instrument_id": underlying.instrument_id, "symbol": underlying.symbol,
             "stream_role": StreamRole.OPTION_CHAIN_QUOTES, "stream_ordinal": 1,
             "raw_bytes": b"raw-options", "normalized_bytes": DataNormalizer.normalized_bytes((snapshot,)),
             "bar_count": 1, "first_bar_start_at": snapshot.canonical_completed_at,
             "last_bar_completed_at": snapshot.canonical_completed_at},
        ),
    )
    rows = db_session.query(HistoricalMarketDatasetSymbol).filter_by(dataset_id=manifest.dataset_id).all()
    assert {(row.symbol, row.stream_role) for row in rows} == {
        (underlying.symbol, "UNDERLYING_SIGNAL_BARS"),
        (underlying.symbol, "OPTION_CHAIN_QUOTES"),
    }
