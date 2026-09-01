import hashlib
import inspect
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect as sa_inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db.models.broker import BrokerAccount
from app.db.models.configuration import CellTreasuryConfig, Instrument, StrategyRegistry
from app.db.models.intelligence import (
    IntelligenceStatefulReplayRun,
    StatefulReplaySessionDelta,
)
from app.db.models.ledger import (
    Fill,
    KairoOrder,
    MarketSnapshot,
    OrderIntent,
)
from app.db.models.projections import CapitalCell
from engine.execution.virtual_clock import ReplayIdentityFactory, VirtualClock
from engine.intelligence.context.context_gate import ContextGate
from engine.intelligence.research.stateful_replay_runner import (
    CANONICAL_AUTHORITIES,
    CanonicalTrackContext,
    CanonicalTrackEvidence,
    CanonicalTradeReference,
    StatefulCounterfactualRunner,
    _TrackSnapshot,
    serialize_stateful_manifest,
)
from engine.risk.governor import RiskGovernor
from engine.risk.models import FillAccountingEvent, RiskSessionSpec
from engine.siphon.models import SyntheticSettlementMetadata


pytestmark = pytest.mark.integration
NOW = datetime(2026, 9, 1, 17, 0, tzinfo=UTC)
START = datetime(2026, 9, 1, 13, 30, tzinfo=UTC)
END = datetime(2026, 9, 1, 20, 0, tzinfo=UTC)
DEFAULT_POLICY_ID = UUID("a0000000-0000-0000-0000-000000000001")


def uid(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"kairo:step5.5-test:{value}")


def seed_stateful_context(
    session: Session,
) -> tuple[CapitalCell, BrokerAccount, Instrument]:
    suffix = uuid4().hex[:10]
    strategy = session.get(StrategyRegistry, ("EMA-CROSS-001", "1.0.0"))
    assert strategy is not None
    instrument = Instrument(
        instrument_id=uuid4(), symbol=f"S{suffix[:8]}",
        asset_class="EQUITY", currency="USD", effective_from=START,
    )
    broker = BrokerAccount(
        broker_account_id=uuid4(), account_key=f"stateful-{suffix}",
        broker_name="STATEFUL_RESEARCH", environment="PAPER",
        status="ACTIVE", effective_from=START,
    )
    session.add_all([instrument, broker])
    session.flush()
    cell = CapitalCell(
        cell_id=uuid4(), cell_code=f"STATEFUL-{suffix}",
        seed_capital=Decimal("200.00"), status="ACTIVE",
        autonomy_tier="APPRENTICE", strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag,
        target_treasury_code=instrument.symbol,
        risk_policy_id=DEFAULT_POLICY_ID, economic_domain="SYNTHETIC",
        updated_at=START,
    )
    session.add(cell)
    session.flush()
    session.add(CellTreasuryConfig(
        config_id=uuid4(), cell_id=cell.cell_id, target_type="SINGLE_ASSET",
        target_instrument_id=instrument.instrument_id,
        target_symbol=instrument.symbol, config_version=1, is_active=True,
        authorized_by="STEP5.5-TEST", created_at=START,
    ))
    session.flush()
    return cell, broker, instrument


class CanonicalDivergenceScenario:
    def __init__(self, cell_id: UUID, broker_id: UUID, instrument_id: UUID) -> None:
        self.cell_id = cell_id
        self.broker_id = broker_id
        self.instrument_id = instrument_id

    def execute(self, context: CanonicalTrackContext) -> CanonicalTrackEvidence:
        track = context.track.value
        session_id = f"STATEFUL-{track}-2026-09-01"
        clock = VirtualClock(START)
        identities = ReplayIdentityFactory(session_id)
        governor = RiskGovernor(
            context.session, cell_id=self.cell_id, clock=clock, identities=identities
        )
        governor.initialize_session(RiskSessionSpec(
            session_id=session_id, trading_date=START.date(),
            session_open=START, session_close=END,
        ))
        clock.advance_to(START + timedelta(microseconds=1))
        governor.arm(authorized_cash_usd=Decimal("200.00"))
        trades: list[CanonicalTradeReference] = []
        siphons: list[UUID] = []

        direct_at = START + timedelta(minutes=30)
        if context.routing.should_route(
            opportunity_key="DIRECT-VETO-001",
            session_date=direct_at.date(),
            counterfactual_opinion="WOULD_HAVE_VETOED",
        ):
            trades.append(self._trade(
                context, governor, clock, "DIRECT-VETO-001", "-6.00", direct_at
            ))

        induced_at = START + timedelta(minutes=45)
        state = governor.current_state()
        if state is not None and state.operational_state == "ARMED" and context.routing.should_route(
            opportunity_key="SUBSEQUENT-001",
            session_date=induced_at.date(),
            counterfactual_opinion="WOULD_HAVE_AUTHORIZED",
        ):
            trades.append(self._trade(
                context, governor, clock, "SUBSEQUENT-001", "10.00", induced_at
            ))
            siphon_id = uid(f"{track}:siphon")
            result = context.siphon.qualify_and_allocate(
                cell_id=self.cell_id,
                occurred_at=induced_at + timedelta(minutes=1),
                synthetic_settled_cash_usd=Decimal("210.00"),
                synthetic_settlement_metadata=SyntheticSettlementMetadata(
                    synthetic_settled_at=induced_at + timedelta(minutes=1),
                    replay_session_id=session_id,
                ),
                siphon_id=siphon_id,
            )
            assert result is not None
            siphons.append(result.siphon_id)

        return CanonicalTrackEvidence(
            trades=tuple(trades), risk_session_ids=(session_id,),
            siphon_ids=tuple(siphons), cell_ids=(self.cell_id,),
        )

    def _trade(
        self,
        context: CanonicalTrackContext,
        governor: RiskGovernor,
        clock: VirtualClock,
        key: str,
        pnl: str,
        occurred_at: datetime,
    ) -> CanonicalTradeReference:
        track = context.track.value
        clock.advance_to(occurred_at)
        intent_id = uid(f"{track}:{key}:intent")
        order_id = uid(f"{track}:{key}:order")
        fill_id = uid(f"{track}:{key}:fill")
        realization_id = uid(f"{track}:{key}:realization")
        snapshot_id = uid(f"{track}:{key}:snapshot")
        context.session.add(MarketSnapshot(
            snapshot_id=snapshot_id, instrument_id=self.instrument_id,
            captured_at=occurred_at, bid=Decimal("1.00"),
            ask=Decimal("1.00"), last=Decimal("1.00"),
            payload={"source": "STATEFUL_RESEARCH_FIXTURE"},
        ))
        context.session.add(OrderIntent(
            intent_id=intent_id, cell_id=self.cell_id,
            strategy_id="EMA-CROSS-001", strategy_version="1.0.0",
            instrument_id=self.instrument_id,
            client_order_key=f"stateful:{track}:{key}",
            order_purpose="TAKE_PROFIT" if Decimal(pnl) >= 0 else "STOP_LOSS",
            side="SELL", target_quantity=Decimal("1"), order_type="MARKET",
            created_at=occurred_at,
        ))
        context.session.flush()
        context.session.add(KairoOrder(
            kairo_order_id=order_id, intent_id=intent_id,
            risk_decision_id=None, broker_account_id=self.broker_id,
            broker_order_id=f"STATEFUL-{track}-{key}",
            status="FILLED", submitted_at=occurred_at,
        ))
        context.session.flush()
        context.session.add(Fill(
            fill_id=fill_id, kairo_order_id=order_id,
            broker_account_id=self.broker_id,
            broker_fill_id=f"STATEFUL-FILL-{track}-{key}",
            instrument_id=self.instrument_id, side="SELL",
            quantity=Decimal("1"), price=Decimal("1"),
            reference_price=Decimal("1"), contract_multiplier=Decimal("1"),
            slippage_usd=Decimal("0"), commission_fee_usd=Decimal("0"),
            is_simulated=True, liquidity_fidelity_tier="TIER_1_QUOTE_DEPTH",
            simulation_model="STEP5.5-CANONICAL-HARNESS",
            simulation_policy_version="1.0.0", source_snapshot_id=snapshot_id,
            simulation_metadata={"synthetic": "true", "execution_guaranteed": False},
            filled_at=occurred_at,
        ))
        context.session.flush()
        context.siphon.record_canonical_realized_pnl(
            fill_id=fill_id, cell_id=self.cell_id, position_effect="CLOSING",
            realized_pnl_usd=Decimal(pnl), occurred_at=occurred_at,
            realization_id=realization_id,
        )
        governor.record_fill_accounting(
            FillAccountingEvent(
                fill_id=fill_id, kairo_order_id=order_id,
                broker_account_id=self.broker_id, instrument_id=self.instrument_id,
                realized_pnl_delta_usd=Decimal(pnl), commission_fees_usd=Decimal("0"),
                slippage_usd=Decimal("0"), fill_price=Decimal("1"),
                filled_qty=Decimal("1"), timestamp=occurred_at,
            ),
            authorized_cash_usd=Decimal("200.00"),
        )
        return CanonicalTradeReference(key, realization_id)


def execute_run(
    session: Session,
) -> tuple[IntelligenceStatefulReplayRun, StatefulCounterfactualRunner, CapitalCell]:
    cell, broker, instrument = seed_stateful_context(session)
    runner = StatefulCounterfactualRunner(session, clock=lambda: NOW)
    result = runner.run(
        cell_id=cell.cell_id, sample_start=START, sample_end=END,
        scenario=CanonicalDivergenceScenario(
            cell.cell_id, broker.broker_account_id, instrument.instrument_id
        ),
    )
    return result, runner, cell


def test_stateful_replay_reuses_canonical_risk_and_accounting_engines(
    db_session: Session,
) -> None:
    _, runner, _ = execute_run(db_session)
    assert CANONICAL_AUTHORITIES["risk"].endswith("RiskGovernor")
    assert CANONICAL_AUTHORITIES["session_replay"].endswith("ReplayOrchestrator")
    assert "KAIRO_PNL_TRACKER" in CANONICAL_AUTHORITIES["pnl"]
    assert runner.last_manifest["canonical_authorities"] == CANONICAL_AUTHORITIES


def test_context_gate_remains_observe_only_during_counterfactual_replay(
    db_session: Session,
) -> None:
    before = ContextGate.authority_mode
    execute_run(db_session)
    assert before == ContextGate.authority_mode == "OBSERVE_ONLY"


def test_research_suppression_exists_only_inside_counterfactual_runner(
    db_session: Session,
) -> None:
    result, runner, _ = execute_run(db_session)
    assert result.direct_vetoed_trades_count == 1
    assert runner.last_manifest["research_semantics"]["routing_disposition"] == (
        "RESEARCH_SUPPRESS"
    )
    assert "RESEARCH_SUPPRESS" not in inspect.getsource(ContextGate)


def test_baseline_and_counterfactual_mutable_state_are_strictly_isolated(
    db_session: Session,
) -> None:
    before_fills = db_session.scalar(select(func.count()).select_from(Fill))
    execute_run(db_session)
    assert db_session.scalar(select(func.count()).select_from(Fill)) == before_fills


def test_stateful_replay_detects_genesis_timing_divergence(db_session: Session) -> None:
    cell, _, _ = seed_stateful_context(db_session)
    buckets = {
        "SAFETY_RESERVE": Decimal("0.00"),
        "TARGET_TREASURY": Decimal("0.00"),
        "REPLICATION_POOL": Decimal("0.00"),
    }
    baseline = _TrackSnapshot(
        trades=(), halt_dates=frozenset(), siphon_buckets=buckets,
        cell_ids=(cell.cell_id,), genesis_session_index=1, suppressions=(),
    )
    counterfactual = _TrackSnapshot(
        trades=(), halt_dates=frozenset(), siphon_buckets=buckets,
        cell_ids=(cell.cell_id,), genesis_session_index=3, suppressions=(),
    )
    runner = StatefulCounterfactualRunner(db_session, clock=lambda: NOW)
    result = runner._reconcile_and_persist(
        cell_id=cell.cell_id, sample_start=START, sample_end=END,
        baseline=baseline, counterfactual=counterfactual,
    )
    assert result.genesis_timing_delta_sessions == 2
    assert runner.last_manifest["divergence"]["genesis_timing_delta_sessions"] == 2


def test_stateful_replay_does_not_create_parallel_pnl_or_siphon_authority(
    db_session: Session,
) -> None:
    execute_run(db_session)
    table_names = set(sa_inspect(db_session.bind).get_table_names())
    assert "stateful_replay_pnl" not in table_names
    assert "stateful_replay_siphons" not in table_names


def test_stateful_replay_tracks_path_dependent_governor_halt_avoidance(
    db_session: Session,
) -> None:
    result, _, _ = execute_run(db_session)
    assert result.baseline_halt_count == 1
    assert result.counterfactual_halt_count == 0


def test_stateful_replay_records_induced_subsequent_trades(db_session: Session) -> None:
    result, runner, _ = execute_run(db_session)
    assert result.induced_trades_taken_count == 1
    assert runner.last_manifest["divergence"]["induced_trades_taken"] == [
        "SUBSEQUENT-001"
    ]


def test_stateful_replay_calculates_exact_cent_siphon_deltas(db_session: Session) -> None:
    result, _, _ = execute_run(db_session)
    assert result.siphon_delta_safety_usd == Decimal("4.00")
    assert result.siphon_delta_treasury_usd == Decimal("4.00")
    assert result.siphon_delta_replication_usd == Decimal("2.00")


def test_stateful_replay_manifest_hash_is_deterministic_and_byte_exact(
    db_session: Session,
) -> None:
    result, runner, cell = execute_run(db_session)
    payload = serialize_stateful_manifest(runner.last_manifest)
    assert hashlib.sha256(payload).hexdigest() == result.stateful_replay_manifest_sha256
    again = StatefulCounterfactualRunner(db_session, clock=lambda: NOW + timedelta(days=1))
    duplicate = again.run(
        cell_id=cell.cell_id, sample_start=START, sample_end=END,
        scenario=CanonicalDivergenceScenario(
            cell.cell_id,
            db_session.scalar(select(BrokerAccount.broker_account_id).where(
                BrokerAccount.broker_name == "STATEFUL_RESEARCH"
            )),
            db_session.scalar(select(Instrument.instrument_id).where(
                Instrument.symbol == cell.target_treasury_code
            )),
        ),
    )
    assert duplicate.replay_run_id == result.replay_run_id
    assert serialize_stateful_manifest(again.last_manifest) == payload


def test_stateful_replay_reconciles_daily_session_deltas(db_session: Session) -> None:
    result, _, _ = execute_run(db_session)
    delta = db_session.scalar(select(StatefulReplaySessionDelta).where(
        StatefulReplaySessionDelta.replay_run_id == result.replay_run_id
    ))
    assert delta.baseline_session_pnl == Decimal("-6.00")
    assert delta.counterfactual_session_pnl == Decimal("10.00")
    assert delta.session_alpha_usd == Decimal("16.00")


def test_database_immutability_rejects_update_or_delete_on_stateful_runs(
    db_session: Session,
) -> None:
    result, _, _ = execute_run(db_session)
    with pytest.raises(DBAPIError, match="immutable"), db_session.begin_nested():
        db_session.execute(text(
            "UPDATE intelligence_stateful_replay_runs "
            "SET stateful_net_alpha_usd = 0 WHERE replay_run_id = :id"
        ), {"id": result.replay_run_id})
    with pytest.raises(DBAPIError, match="immutable"), db_session.begin_nested():
        db_session.execute(text(
            "DELETE FROM intelligence_stateful_replay_runs WHERE replay_run_id = :id"
        ), {"id": result.replay_run_id})


def test_zero_runtime_trade_authority_or_governor_leakage_from_stateful_replay(
    db_session: Session,
) -> None:
    before_intents = db_session.scalar(select(func.count()).select_from(OrderIntent))
    execute_run(db_session)
    assert db_session.scalar(select(func.count()).select_from(OrderIntent)) == before_intents
    assert StatefulCounterfactualRunner.authority_mode == "OFFLINE_RESEARCH_ONLY"
    assert ContextGate.authority_mode == "OBSERVE_ONLY"


def test_migration_0021_upgrade_and_downgrade_are_clean_and_data_safe(
    migrated_database: tuple[str, str],
) -> None:
    admin_url, _ = migrated_database
    backend_root = Path(__file__).resolve().parents[3]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    command.downgrade(config, "0020")
    engine = create_engine(admin_url)
    created_cell = False
    try:
        assert "intelligence_stateful_replay_runs" not in sa_inspect(engine).get_table_names()
        command.upgrade(config, "0021")
        assert "intelligence_stateful_replay_runs" in sa_inspect(engine).get_table_names()
        with engine.begin() as connection:
            cell_id = connection.execute(
                text("SELECT cell_id FROM capital_cells ORDER BY cell_code LIMIT 1")
            ).scalar_one_or_none()
            if cell_id is None:
                strategy = connection.execute(text(
                    "SELECT strategy_id, version_tag FROM strategy_registry LIMIT 1"
                )).one()
                cell_id = uuid4()
                created_cell = True
                connection.execute(text("""
                    INSERT INTO capital_cells (
                        cell_id, cell_code, seed_capital, status, autonomy_tier,
                        strategy_id, strategy_version, target_treasury_code,
                        risk_policy_id, economic_domain, updated_at
                    ) VALUES (
                        :id, :code, 100, 'ACTIVE', 'APPRENTICE', :strategy,
                        :version, 'QQQ', :policy, 'SYNTHETIC', :now
                    )
                """), {
                    "id": cell_id, "code": f"MIG-{str(cell_id)[:8]}",
                    "strategy": strategy[0], "version": strategy[1],
                    "policy": DEFAULT_POLICY_ID, "now": NOW,
                })
            connection.execute(text("""
                INSERT INTO intelligence_stateful_replay_runs (
                    replay_run_id, cell_id, research_method, sample_start_time,
                    sample_end_time, baseline_trade_count, counterfactual_trade_count,
                    direct_vetoed_trades_count, induced_trades_taken_count,
                    induced_trades_missed_count, baseline_net_pnl,
                    counterfactual_net_pnl, stateful_net_alpha_usd,
                    baseline_max_drawdown_usd, counterfactual_max_drawdown_usd,
                    drawdown_reduction_usd, baseline_halt_count,
                    counterfactual_halt_count, siphon_delta_treasury_usd,
                    siphon_delta_replication_usd, siphon_delta_safety_usd,
                    baseline_cell_count, counterfactual_cell_count,
                    genesis_timing_delta_sessions, stateful_replay_manifest_sha256,
                    executed_at
                ) VALUES (
                    :run, :cell, 'STATEFUL_REPLAY_COUNTERFACTUAL', :start, :end,
                    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                    1, 1, NULL, :hash, :now
                )
            """), {
                "run": uuid4(), "cell": cell_id, "start": START, "end": END,
                "hash": "0" * 64, "now": NOW,
            })
        with pytest.raises(Exception, match="Refusing 0021 downgrade"):
            command.downgrade(config, "0020")
        with engine.begin() as connection:
            connection.execute(text(
                "TRUNCATE stateful_replay_session_deltas, "
                "intelligence_stateful_replay_runs"
            ))
        command.downgrade(config, "0020")
        if created_cell:
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM capital_cells WHERE cell_id = :cell_id"),
                    {"cell_id": cell_id},
                )
        command.upgrade(config, "head")
    finally:
        engine.dispose()
