import hashlib
import inspect as pyinspect
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from sqlalchemy.orm import Session

from app.db.models.broker import BrokerAccount
from app.db.models.configuration import Instrument, StrategyRegistry
from app.db.models.historical import HistoricalMarketArtifact, HistoricalMarketDataset, HistoricalMarketDatasetSymbol
from app.db.models.ledger import KairoCapitalAuthorizationRecord, SyntheticEvidenceManifest
from app.db.models.projections import CapitalCell
from app.db.models.risk import RiskSession
from engine.execution.replay_orchestrator import ReplayRunResult
from engine.validation.feed_loader import DataNormalizer
from engine.validation.models import CanonicalMarketBar
from engine.validation.multi_year_runner import MultiYearReplayConfig, MultiYearReplayRunner, REPLAY_AUTHORITY
from engine.validation.stream_loader import HistoricalDatasetStreamLoader

pytestmark = pytest.mark.integration
UTC = timezone.utc
NOW = datetime(2026, 3, 9, 13, 30, tzinfo=UTC)
POLICY_ID = UUID("a0000000-0000-0000-0000-000000000001")


def uid(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"kairo-step45-2:{value}")


class NoopLifecycle:
    def __init__(self): self.calls = []
    def finalize(self, **kwargs): self.calls.append(kwargs)


class FakeOrchestrator:
    calls = []
    missing = False
    def __init__(self, session, config): self.config = config; self.__class__.calls.append(config)
    def replay_research(self, streams):
        stamp = f"{self.config.session_id}:{len(streams)}"
        return ReplayRunResult(
            manifest_hash=hashlib.sha256(stamp.encode()).hexdigest(), manifest_id=uid(stamp),
            financial_ids=(), event_count=sum(len(row.events) for row in streams), lineage=tuple(row.provider.lineage for row in streams),
            missing_execution_evidence=({"timestamp": self.config.session_open.isoformat(), "symbol": "TQQQ", "reason": "INSUFFICIENT_EXECUTION_EVIDENCE", "detail": "ENTRY_OPTION_QUOTE_MISSING"},) if self.missing else (),
        )


@pytest.fixture
def multi_year_case(db_session: Session):
    strategy = db_session.get(StrategyRegistry, ("EMA-CROSS-001", "1.0.0")); assert strategy
    instrument = Instrument(instrument_id=uid("TQQQ"), symbol="TQQQ", asset_class="EQUITY", currency="USD", effective_from=NOW)
    broker = BrokerAccount(broker_account_id=uid("paper"), account_key="MYR-PAPER", broker_name="TEST", environment="PAPER", status="ACTIVE", effective_from=NOW)
    cell = CapitalCell(
        cell_id=uid("cell"), cell_code="MYR-A001", seed_capital=Decimal("100"), status="ACTIVE",
        autonomy_tier="APPRENTICE", strategy_id=strategy.strategy_id, strategy_version=strategy.version_tag,
        target_treasury_code=instrument.symbol, risk_policy_id=POLICY_ID, economic_domain="SYNTHETIC", updated_at=NOW,
    )
    db_session.add_all([instrument, broker, cell]); db_session.flush()
    provenance = SyntheticEvidenceManifest(
        manifest_id=uid("provenance"), manifest_type="REPLAY_RUN", manifest_hash="1" * 64,
        manifest_algorithm="REPLAY-MANIFEST-v1", cell_id=cell.cell_id, source_count=0, source_refs={},
        model_identifier="EMA-CROSS-001", model_version="1.0.0", created_at=NOW,
    )
    db_session.add(provenance); db_session.flush()
    db_session.add(KairoCapitalAuthorizationRecord(
        authorization_id=uid("capital"), cell_id=cell.cell_id, broker_snapshot_id=None,
        broker_account_id=None, economic_domain="SYNTHETIC", synthetic_provenance_id=provenance.manifest_id,
        settled_cash=Decimal("100"), safety_reserve=Decimal("0"), ownership_treasury_reserved=Decimal("0"),
        replication_reserve=Decimal("0"), committed_obligations=Decimal("0"),
        authorized_trading_cash=Decimal("100"), computed_at=NOW,
    )); db_session.flush()
    bars = tuple(
        CanonicalMarketBar(
            instrument_id=instrument.instrument_id, symbol=instrument.symbol,
            interval_start_at=datetime(2026,3,day,13,30,tzinfo=UTC)+timedelta(minutes=index),
            completed_at=datetime(2026,3,day,13,31,tzinfo=UTC)+timedelta(minutes=index),
            open=Decimal("11") if index==9 else Decimal("10"),
            high=Decimal("11") if index==9 else Decimal("10"), low=Decimal("10"),
            close=Decimal("11") if index==9 else Decimal("10"), volume=None,
        )
        for day in (9,10) for index in range(10)
    )
    normalized = DataNormalizer.normalized_bytes(bars); raw=b"raw-multi-year"
    raw_artifact = HistoricalMarketArtifact(artifact_id=uid("raw"), artifact_role="RAW_PROVIDER_PAYLOAD", content_sha256=hashlib.sha256(raw).hexdigest(), mime_type="text/csv", byte_size=len(raw), storage_uri="file:///raw", created_at=NOW)
    norm_artifact = HistoricalMarketArtifact(artifact_id=uid("norm"), artifact_role="NORMALIZED_RESEARCH_STREAM", content_sha256=hashlib.sha256(normalized).hexdigest(), mime_type="application/json", byte_size=len(normalized), storage_uri="file:///norm", created_at=NOW)
    dataset = HistoricalMarketDataset(
        dataset_id=uid("dataset"), dataset_name="MYR-FIXTURE", provider_name="FIXTURE", bar_interval_seconds=60,
        source_timezone="America/New_York", calendar_name="XNYS", calendar_version="CAL-US-EQUITIES-2026-v1",
        source_timestamp_convention="INTERVAL_BEGIN", liquidity_fidelity_tier="TIER_3_BAR_ONLY",
        price_adjustment_mode="RAW_UNADJUSTED", adjustment_policy_version=None,
        normalization_policy_version="NORM-BAR-UTC-CAUSAL-v1", coverage_start=bars[0].interval_start_at,
        coverage_end=bars[-1].completed_at, dataset_manifest_sha256="2"*64, ingested_at=NOW,
    )
    db_session.add_all([raw_artifact,norm_artifact,dataset]); db_session.flush()
    db_session.add(HistoricalMarketDatasetSymbol(
        symbol_entry_id=uid("stream"), dataset_id=dataset.dataset_id, instrument_id=instrument.instrument_id,
        symbol=instrument.symbol, stream_role="UNDERLYING_SIGNAL_BARS", stream_ordinal=7,
        raw_artifact_id=raw_artifact.artifact_id, raw_content_sha256=raw_artifact.content_sha256,
        normalized_artifact_id=norm_artifact.artifact_id, normalized_content_sha256=norm_artifact.content_sha256,
        bar_count=len(bars), first_bar_start_at=bars[0].interval_start_at, last_bar_completed_at=bars[-1].completed_at,
    )); db_session.flush()
    reader=lambda artifact: {norm_artifact.artifact_id:normalized}[artifact.artifact_id]
    loader=HistoricalDatasetStreamLoader(db_session,reader)
    def config(version="1.0.0"):
        return MultiYearReplayConfig(
            dataset_id=dataset.dataset_id, cell_id=cell.cell_id, broker_account_id=broker.broker_account_id,
            start_date=date(2026,3,9), end_date=date(2026,3,10), strategy_version=version,
            strategy_parameters_sha256=hashlib.sha256(version.encode()).hexdigest(), engine_versions={"kairo":"step45-2"},
        )
    return db_session,cell,broker,dataset,loader,config


def _run(case, *, version="1.0.0", missing=False):
    session,cell,broker,dataset,loader,config=case; lifecycle=NoopLifecycle(); FakeOrchestrator.calls=[]; FakeOrchestrator.missing=missing
    runner=MultiYearReplayRunner(session,config(version),stream_loader=loader,lifecycle=lifecycle,orchestrator_factory=FakeOrchestrator)
    return runner,runner.run(),lifecycle


def test_multi_year_runner_does_not_reimplement_canonical_cash_or_pnl_math(multi_year_case):
    runner,result,lifecycle=_run(multi_year_case)
    source=pyinspect.getsource(MultiYearReplayRunner.run)
    assert "settled_cash +" not in source and "session_net_pnl =" not in source
    assert len(lifecycle.calls)==2 and result.sessions_processed==2


def test_cross_session_state_is_loaded_from_canonical_persisted_authorities(multi_year_case):
    runner,result,_=_run(multi_year_case)
    assert len(runner.persisted_state_trace)==2
    assert {row["authorization_id"] for row in runner.persisted_state_trace}=={str(uid("capital"))}


def test_historical_genesis_uses_explicit_replay_simulated_human_authorization():
    from engine.validation.multi_year_runner import CanonicalPostSessionLifecycle
    assert REPLAY_AUTHORITY=="REPLAY_SIMULATED_HUMAN_AUTHORIZATION"
    assert "authorized_by=REPLAY_AUTHORITY" in pyinspect.getsource(CanonicalPostSessionLifecycle.finalize)


def test_stream_order_is_derived_exclusively_from_dataset_stream_ordinal(multi_year_case):
    session,cell,broker,dataset,loader,config=multi_year_case
    rows=loader.load(dataset.dataset_id)
    assert [row.stream_ordinal for row in rows]==sorted(row.stream_ordinal for row in rows)
    assert "order_by(HistoricalMarketDatasetSymbol.stream_ordinal)" in pyinspect.getsource(HistoricalDatasetStreamLoader.load)


def test_missing_option_execution_evidence_fails_trade_closed_without_forward_fill(multi_year_case):
    session,cell,broker,dataset,loader,config=multi_year_case
    result=MultiYearReplayRunner(session,config(),stream_loader=loader,lifecycle=NoopLifecycle()).run()
    assert result.missing_option_bars==2 and result.skipped_executions==2
    assert result.evidence_manifest.payload["execution_metrics"]["trade_count"]==0


def test_multi_year_manifest_binds_dataset_strategy_risk_and_normalization_identity(multi_year_case):
    _,result,_=_run(multi_year_case); payload=result.evidence_manifest.payload
    assert payload["dataset_manifest_sha256"]=="2"*64
    assert payload["strategy_identity"]["strategy_id"]=="EMA-CROSS-001"
    assert payload["risk_policy_identity"]["loss_floor"].startswith("-6")
    assert payload["normalization_policy_version"]=="NORM-BAR-UTC-CAUSAL-v1"


def test_same_market_data_with_different_strategy_version_produces_different_manifest(multi_year_case):
    session,*_=multi_year_case
    base=session.get(StrategyRegistry,("EMA-CROSS-001","1.0.0"))
    session.add(StrategyRegistry(strategy_id=base.strategy_id,version_tag="1.0.1",display_name=base.display_name,status="ACTIVE",configuration=base.configuration,registered_at=NOW)); session.flush()
    _,first,_=_run(multi_year_case,version="1.0.0")
    _,second,_=_run(multi_year_case,version="1.0.1")
    assert first.evidence_manifest.multi_year_manifest_sha256!=second.evidence_manifest.multi_year_manifest_sha256


def test_multi_year_replay_enforces_daily_governor_loss_floor_per_session(multi_year_case):
    session,cell,broker,dataset,loader,config=multi_year_case; lifecycle=NoopLifecycle()
    runner=MultiYearReplayRunner(session,config(),stream_loader=loader,lifecycle=lifecycle)
    result=runner.run()
    assert result.evidence_manifest.payload["risk_policy_identity"]["loss_floor"]=="-6.0000000000"
    sessions=list(session.query(RiskSession).filter(RiskSession.cell_id==cell.cell_id).all())
    assert len(sessions)==2 and len({row.session_id for row in sessions})==2


def test_multi_year_runner_executes_without_altering_live_broker_authority(multi_year_case):
    session,*_=multi_year_case
    live=BrokerAccount(broker_account_id=uid("live"),account_key="MYR-LIVE",broker_name="TEST",environment="LIVE",status="ACTIVE",effective_from=NOW)
    session.add(live); session.flush(); before=(live.environment,live.status,live.retired_at)
    _run(multi_year_case); session.refresh(live)
    assert (live.environment,live.status,live.retired_at)==before
