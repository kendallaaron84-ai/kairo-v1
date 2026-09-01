import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.intelligence import (
    IntelligenceStatefulReplayRun,
    StatefulReplaySessionDelta,
)
from app.db.models.ledger import Fill, FillRealizedPnL, SiphonAllocation, SiphonEvent
from app.db.models.projections import CapitalCell
from app.db.models.replication import CellGenesisEvent
from app.db.models.risk import RiskSession, RiskStateEvent
from engine.execution.paper_broker import PaperExecutionEngine
from engine.execution.replay_orchestrator import ReplayOrchestrator
from engine.replay.flywheel_runner import FlywheelRunner
from engine.replication.genesis_factory import GenesisFactory
from engine.replication.replication_manager import ReplicationManager
from engine.risk.governor import RiskGovernor
from engine.risk.pnl_tracker import realized_round_trip_pnl
from engine.siphon.siphon_manager import SiphonManager
from engine.strategy.ema_cross_strategy import EMACrossStrategy
from engine.treasury.treasury_manager import TreasuryManager


CENT = Decimal("0.01")
RESEARCH_METHOD = "STATEFUL_REPLAY_COUNTERFACTUAL"
_RESEARCH_SUPPRESS = "RESEARCH_SUPPRESS"

CANONICAL_AUTHORITIES = {
    "strategy": f"{EMACrossStrategy.__module__}.{EMACrossStrategy.__name__}",
    "session_replay": f"{ReplayOrchestrator.__module__}.{ReplayOrchestrator.__name__}",
    "flywheel": f"{FlywheelRunner.__module__}.{FlywheelRunner.__name__}",
    "risk": f"{RiskGovernor.__module__}.{RiskGovernor.__name__}",
    "execution": f"{PaperExecutionEngine.__module__}.{PaperExecutionEngine.__name__}",
    "pnl": (
        f"{realized_round_trip_pnl.__module__}.{realized_round_trip_pnl.__name__}"
        "+FillRealizedPnL/KAIRO_PNL_TRACKER"
    ),
    "siphon": f"{SiphonManager.__module__}.{SiphonManager.__name__}",
    "treasury": f"{TreasuryManager.__module__}.{TreasuryManager.__name__}",
    "replication": f"{ReplicationManager.__module__}.{ReplicationManager.__name__}",
    "genesis": f"{GenesisFactory.__module__}.{GenesisFactory.__name__}",
}


class ReplayTrack(StrEnum):
    BASELINE = "BASELINE"
    COUNTERFACTUAL = "COUNTERFACTUAL"


@dataclass(frozen=True)
class CanonicalTradeReference:
    opportunity_key: str
    realization_id: UUID


@dataclass(frozen=True)
class CanonicalTrackEvidence:
    """References to canonical facts created inside one isolated replay track."""

    trades: tuple[CanonicalTradeReference, ...]
    risk_session_ids: tuple[str, ...]
    siphon_ids: tuple[UUID, ...]
    cell_ids: tuple[UUID, ...]
    genesis_event_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class SuppressionFact:
    opportunity_key: str
    session_date: date
    disposition: str = _RESEARCH_SUPPRESS


class ResearchRoutingHarness:
    """Track-local routing decision; it never mutates ContextGate authority."""

    def __init__(self, track: ReplayTrack) -> None:
        self.track = track
        self._suppressions: list[SuppressionFact] = []

    @property
    def suppressions(self) -> tuple[SuppressionFact, ...]:
        return tuple(self._suppressions)

    def should_route(
        self,
        *,
        opportunity_key: str,
        session_date: date,
        counterfactual_opinion: str | None,
    ) -> bool:
        if (
            self.track is ReplayTrack.COUNTERFACTUAL
            and counterfactual_opinion == "WOULD_HAVE_VETOED"
        ):
            self._suppressions.append(SuppressionFact(opportunity_key, session_date))
            return False
        return True


@dataclass(frozen=True)
class CanonicalTrackContext:
    session: Session
    track: ReplayTrack
    routing: ResearchRoutingHarness
    flywheel: FlywheelRunner
    siphon: SiphonManager
    treasury: TreasuryManager
    replication: ReplicationManager
    genesis: GenesisFactory


class CanonicalStatefulScenario(Protocol):
    """Scenario adapter that invokes Kairo's canonical components only."""

    def execute(self, context: CanonicalTrackContext) -> CanonicalTrackEvidence: ...


@dataclass(frozen=True)
class _TradeFact:
    opportunity_key: str
    realization_id: UUID
    fill_id: UUID
    pnl: Decimal
    occurred_at: datetime


@dataclass(frozen=True)
class _TrackSnapshot:
    trades: tuple[_TradeFact, ...]
    halt_dates: frozenset[date]
    siphon_buckets: dict[str, Decimal]
    cell_ids: tuple[UUID, ...]
    genesis_session_index: int | None
    suppressions: tuple[SuppressionFact, ...]


def _money(value: Decimal | int | str) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_EVEN)


def _max_drawdown(trades: tuple[_TradeFact, ...]) -> Decimal:
    equity = peak = maximum = Decimal("0.00")
    for trade in trades:
        equity += trade.pnl
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return _money(maximum)


def serialize_stateful_manifest(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


class StatefulCounterfactualRunner:
    """Runs two canonical tracks in rollback-isolated database savepoints."""

    authority_mode = "OFFLINE_RESEARCH_ONLY"
    research_method = RESEARCH_METHOD

    def __init__(
        self,
        session: Session,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = session
        self.clock = clock or (lambda: datetime.now(UTC))
        self.last_manifest: dict[str, object] | None = None

    def run(
        self,
        *,
        cell_id: UUID,
        sample_start: datetime,
        sample_end: datetime,
        scenario: CanonicalStatefulScenario,
    ) -> IntelligenceStatefulReplayRun:
        self._aware(sample_start, "sample_start")
        self._aware(sample_end, "sample_end")
        if sample_end < sample_start:
            raise ValueError("sample_end cannot precede sample_start")
        if self.db.get(CapitalCell, cell_id) is None:
            raise ValueError("stateful replay root cell does not resolve")

        baseline = self._execute_isolated(
            ReplayTrack.BASELINE, scenario, sample_start, sample_end
        )
        counterfactual = self._execute_isolated(
            ReplayTrack.COUNTERFACTUAL, scenario, sample_start, sample_end
        )
        return self._reconcile_and_persist(
            cell_id=cell_id,
            sample_start=sample_start,
            sample_end=sample_end,
            baseline=baseline,
            counterfactual=counterfactual,
        )

    def _execute_isolated(
        self,
        track: ReplayTrack,
        scenario: CanonicalStatefulScenario,
        sample_start: datetime,
        sample_end: datetime,
    ) -> _TrackSnapshot:
        savepoint = self.db.begin_nested()
        routing = ResearchRoutingHarness(track)
        identity = lambda record_type, key: uuid5(
            NAMESPACE_URL, f"kairo:stateful-track:{track.value}:{record_type}:{key}"
        )
        context = CanonicalTrackContext(
            session=self.db,
            track=track,
            routing=routing,
            flywheel=FlywheelRunner(self.db),
            siphon=SiphonManager(self.db, identity_factory=identity),
            treasury=TreasuryManager(self.db, identity_factory=identity),
            replication=ReplicationManager(self.db),
            genesis=GenesisFactory(self.db),
        )
        try:
            evidence = scenario.execute(context)
            self.db.flush()
            snapshot = self._snapshot(
                evidence, routing.suppressions, sample_start, sample_end
            )
        finally:
            if savepoint.is_active:
                savepoint.rollback()
            self.db.expire_all()
        return snapshot

    def _snapshot(
        self,
        evidence: CanonicalTrackEvidence,
        suppressions: tuple[SuppressionFact, ...],
        sample_start: datetime,
        sample_end: datetime,
    ) -> _TrackSnapshot:
        if not evidence.cell_ids:
            raise ValueError("each replay track requires at least one canonical cell")
        if len(set(evidence.cell_ids)) != len(evidence.cell_ids):
            raise ValueError("track cell identities must be unique")
        for cell_id in evidence.cell_ids:
            if self.db.get(CapitalCell, cell_id) is None:
                raise ValueError(f"track cell {cell_id} does not resolve")

        trades: list[_TradeFact] = []
        opportunities: set[str] = set()
        for reference in evidence.trades:
            if not reference.opportunity_key or reference.opportunity_key in opportunities:
                raise ValueError("track trade opportunity keys must be non-empty and unique")
            opportunities.add(reference.opportunity_key)
            fact = self.db.get(FillRealizedPnL, reference.realization_id)
            if fact is None or fact.position_effect != "CLOSING":
                raise ValueError("trade reference does not resolve to canonical closing P&L")
            if fact.source_authority != "KAIRO_PNL_TRACKER":
                raise ValueError("stateful research accepts only canonical KAIRO_PNL_TRACKER facts")
            fill = self.db.get(Fill, fact.fill_id)
            if fill is None or not fill.is_simulated:
                raise ValueError("stateful replay trade must resolve to a simulated canonical fill")
            if not sample_start <= fact.occurred_at <= sample_end:
                raise ValueError("canonical trade fact falls outside the research window")
            trades.append(_TradeFact(
                opportunity_key=reference.opportunity_key,
                realization_id=fact.realization_id,
                fill_id=fact.fill_id,
                pnl=_money(fact.realized_pnl_usd),
                occurred_at=fact.occurred_at,
            ))
        trades.sort(key=lambda row: (row.occurred_at, row.fill_id, row.realization_id))

        sessions: list[RiskSession] = []
        for session_id in evidence.risk_session_ids:
            row = self.db.get(RiskSession, session_id)
            if row is None or row.cell_id not in evidence.cell_ids:
                raise ValueError("risk session does not resolve inside the track cell namespace")
            sessions.append(row)
        session_dates = sorted({row.trading_date for row in sessions})
        events = list(self.db.scalars(
            select(RiskStateEvent).where(
                RiskStateEvent.session_id.in_(evidence.risk_session_ids)
            )
        )) if evidence.risk_session_ids else []
        halt_dates = frozenset(
            next(row.trading_date for row in sessions if row.session_id == event.session_id)
            for event in events
            if event.new_state in {"HALTED_HARD", "LOCKED_FOR_DAY", "FLAT_LOCKED"}
        )

        buckets = {name: Decimal("0.00") for name in (
            "SAFETY_RESERVE", "TARGET_TREASURY", "REPLICATION_POOL"
        )}
        for siphon_id in evidence.siphon_ids:
            siphon = self.db.get(SiphonEvent, siphon_id)
            if siphon is None or siphon.cell_id not in evidence.cell_ids:
                raise ValueError("siphon reference does not resolve inside the track")
            for allocation in self.db.scalars(
                select(SiphonAllocation).where(SiphonAllocation.siphon_id == siphon_id)
            ):
                buckets[allocation.bucket_type] += _money(allocation.allocated_usd)

        genesis_indices: list[int] = []
        for genesis_id in evidence.genesis_event_ids:
            genesis = self.db.get(CellGenesisEvent, genesis_id)
            if (
                genesis is None
                or genesis.parent_cell_id not in evidence.cell_ids
                or genesis.child_cell_id not in evidence.cell_ids
            ):
                raise ValueError("genesis reference does not resolve inside the track")
            later_sessions = [
                index for index, session_date in enumerate(session_dates, start=1)
                if session_date >= genesis.occurred_at.date()
            ]
            if later_sessions:
                genesis_indices.append(later_sessions[0])

        return _TrackSnapshot(
            trades=tuple(trades),
            halt_dates=halt_dates,
            siphon_buckets={key: _money(value) for key, value in buckets.items()},
            cell_ids=tuple(sorted(evidence.cell_ids, key=str)),
            genesis_session_index=min(genesis_indices) if genesis_indices else None,
            suppressions=suppressions,
        )

    def _reconcile_and_persist(
        self,
        *,
        cell_id: UUID,
        sample_start: datetime,
        sample_end: datetime,
        baseline: _TrackSnapshot,
        counterfactual: _TrackSnapshot,
    ) -> IntelligenceStatefulReplayRun:
        baseline_keys = {row.opportunity_key for row in baseline.trades}
        counterfactual_keys = {row.opportunity_key for row in counterfactual.trades}
        suppressed_keys = {row.opportunity_key for row in counterfactual.suppressions}
        induced_taken = sorted(counterfactual_keys - baseline_keys)
        induced_missed = sorted((baseline_keys - counterfactual_keys) - suppressed_keys)
        direct_vetoed = sorted(suppressed_keys)

        baseline_pnl = _money(sum((row.pnl for row in baseline.trades), Decimal("0")))
        counterfactual_pnl = _money(
            sum((row.pnl for row in counterfactual.trades), Decimal("0"))
        )
        baseline_dd = _max_drawdown(baseline.trades)
        counterfactual_dd = _max_drawdown(counterfactual.trades)
        session_payload = self._session_payload(
            baseline, counterfactual, suppressed_keys, set(induced_taken)
        )
        genesis_delta = (
            counterfactual.genesis_session_index - baseline.genesis_session_index
            if baseline.genesis_session_index is not None
            and counterfactual.genesis_session_index is not None
            else None
        )
        base_manifest: dict[str, object] = {
            "authority_mode": self.authority_mode,
            "research_method": self.research_method,
            "research_semantics": {
                "context_gate_authority": "OBSERVE_ONLY",
                "routing_disposition_scope": "COUNTERFACTUAL_HARNESS_ONLY",
                "routing_disposition": _RESEARCH_SUPPRESS,
                "claims_statistical_significance": False,
            },
            "canonical_authorities": CANONICAL_AUTHORITIES,
            "cell_id": str(cell_id),
            "sample_start": sample_start.isoformat(),
            "sample_end": sample_end.isoformat(),
            "baseline": self._track_manifest(baseline),
            "counterfactual": self._track_manifest(counterfactual),
            "divergence": {
                "direct_vetoed_opportunities": direct_vetoed,
                "induced_trades_taken": induced_taken,
                "induced_trades_missed": induced_missed,
                "stateful_net_alpha_usd": str(counterfactual_pnl - baseline_pnl),
                "drawdown_reduction_usd": str(baseline_dd - counterfactual_dd),
                "siphon_delta_safety_usd": str(
                    counterfactual.siphon_buckets["SAFETY_RESERVE"]
                    - baseline.siphon_buckets["SAFETY_RESERVE"]
                ),
                "siphon_delta_treasury_usd": str(
                    counterfactual.siphon_buckets["TARGET_TREASURY"]
                    - baseline.siphon_buckets["TARGET_TREASURY"]
                ),
                "siphon_delta_replication_usd": str(
                    counterfactual.siphon_buckets["REPLICATION_POOL"]
                    - baseline.siphon_buckets["REPLICATION_POOL"]
                ),
                "genesis_timing_delta_sessions": genesis_delta,
            },
            "session_deltas": session_payload,
        }
        lineage_hash = hashlib.sha256(serialize_stateful_manifest(base_manifest)).hexdigest()
        run_id = uuid5(NAMESPACE_URL, f"kairo:stateful-replay:{lineage_hash}")
        manifest = {"replay_run_id": str(run_id), **base_manifest}
        manifest_hash = hashlib.sha256(serialize_stateful_manifest(manifest)).hexdigest()
        self.last_manifest = manifest
        existing = self.db.get(IntelligenceStatefulReplayRun, run_id)
        if existing is not None:
            if existing.stateful_replay_manifest_sha256 != manifest_hash:
                raise ValueError("conflicting deterministic stateful replay identity")
            return existing

        executed_at = self.clock()
        self._aware(executed_at, "research clock")
        row = IntelligenceStatefulReplayRun(
            replay_run_id=run_id,
            cell_id=cell_id,
            research_method=self.research_method,
            sample_start_time=sample_start,
            sample_end_time=sample_end,
            baseline_trade_count=len(baseline.trades),
            counterfactual_trade_count=len(counterfactual.trades),
            direct_vetoed_trades_count=len(direct_vetoed),
            induced_trades_taken_count=len(induced_taken),
            induced_trades_missed_count=len(induced_missed),
            baseline_net_pnl=baseline_pnl,
            counterfactual_net_pnl=counterfactual_pnl,
            stateful_net_alpha_usd=counterfactual_pnl - baseline_pnl,
            baseline_max_drawdown_usd=baseline_dd,
            counterfactual_max_drawdown_usd=counterfactual_dd,
            drawdown_reduction_usd=baseline_dd - counterfactual_dd,
            baseline_halt_count=len(baseline.halt_dates),
            counterfactual_halt_count=len(counterfactual.halt_dates),
            siphon_delta_treasury_usd=(
                counterfactual.siphon_buckets["TARGET_TREASURY"]
                - baseline.siphon_buckets["TARGET_TREASURY"]
            ),
            siphon_delta_replication_usd=(
                counterfactual.siphon_buckets["REPLICATION_POOL"]
                - baseline.siphon_buckets["REPLICATION_POOL"]
            ),
            siphon_delta_safety_usd=(
                counterfactual.siphon_buckets["SAFETY_RESERVE"]
                - baseline.siphon_buckets["SAFETY_RESERVE"]
            ),
            baseline_cell_count=len(baseline.cell_ids),
            counterfactual_cell_count=len(counterfactual.cell_ids),
            genesis_timing_delta_sessions=genesis_delta,
            stateful_replay_manifest_sha256=manifest_hash,
            executed_at=executed_at,
        )
        self.db.add(row)
        self.db.flush()
        for payload in session_payload:
            self.db.add(StatefulReplaySessionDelta(
                session_delta_id=uuid5(
                    NAMESPACE_URL,
                    f"kairo:stateful-session:{run_id}:{payload['session_date']}",
                ),
                replay_run_id=run_id,
                session_date=date.fromisoformat(str(payload["session_date"])),
                baseline_session_pnl=Decimal(str(payload["baseline_session_pnl"])),
                counterfactual_session_pnl=Decimal(str(payload["counterfactual_session_pnl"])),
                session_alpha_usd=Decimal(str(payload["session_alpha_usd"])),
                baseline_halted=bool(payload["baseline_halted"]),
                counterfactual_halted=bool(payload["counterfactual_halted"]),
                vetoed_in_session_count=int(payload["vetoed_in_session_count"]),
                induced_in_session_count=int(payload["induced_in_session_count"]),
            ))
        self.db.flush()
        return row

    @staticmethod
    def _track_manifest(snapshot: _TrackSnapshot) -> dict[str, object]:
        net = _money(sum((row.pnl for row in snapshot.trades), Decimal("0")))
        return {
            "trade_count": len(snapshot.trades),
            "net_pnl": str(net),
            "max_drawdown_usd": str(_max_drawdown(snapshot.trades)),
            "halt_count": len(snapshot.halt_dates),
            "cell_ids": [str(item) for item in snapshot.cell_ids],
            "genesis_session_index": snapshot.genesis_session_index,
            "siphon_buckets": {
                key: str(snapshot.siphon_buckets[key]) for key in sorted(snapshot.siphon_buckets)
            },
            "trades": [
                {
                    "opportunity_key": row.opportunity_key,
                    "realization_id": str(row.realization_id),
                    "fill_id": str(row.fill_id),
                    "pnl": str(row.pnl),
                    "occurred_at": row.occurred_at.isoformat(),
                }
                for row in snapshot.trades
            ],
        }

    @staticmethod
    def _session_payload(
        baseline: _TrackSnapshot,
        counterfactual: _TrackSnapshot,
        suppressed: set[str],
        induced: set[str],
    ) -> list[dict[str, object]]:
        baseline_by_date: dict[date, list[_TradeFact]] = defaultdict(list)
        counterfactual_by_date: dict[date, list[_TradeFact]] = defaultdict(list)
        for row in baseline.trades:
            baseline_by_date[row.occurred_at.date()].append(row)
        for row in counterfactual.trades:
            counterfactual_by_date[row.occurred_at.date()].append(row)
        suppression_dates = {row.opportunity_key: row.session_date for row in counterfactual.suppressions}
        dates = sorted(
            set(baseline_by_date)
            | set(counterfactual_by_date)
            | baseline.halt_dates
            | counterfactual.halt_dates
            | set(suppression_dates.values())
        )
        payload: list[dict[str, object]] = []
        for session_date in dates:
            baseline_pnl = _money(sum(
                (row.pnl for row in baseline_by_date[session_date]), Decimal("0")
            ))
            counterfactual_pnl = _money(sum(
                (row.pnl for row in counterfactual_by_date[session_date]), Decimal("0")
            ))
            payload.append({
                "session_date": session_date.isoformat(),
                "baseline_session_pnl": str(baseline_pnl),
                "counterfactual_session_pnl": str(counterfactual_pnl),
                "session_alpha_usd": str(counterfactual_pnl - baseline_pnl),
                "baseline_halted": session_date in baseline.halt_dates,
                "counterfactual_halted": session_date in counterfactual.halt_dates,
                "vetoed_in_session_count": sum(
                    key in suppressed and value == session_date
                    for key, value in suppression_dates.items()
                ),
                "induced_in_session_count": sum(
                    row.opportunity_key in induced
                    for row in counterfactual_by_date[session_date]
                ),
            })
        return payload

    @staticmethod
    def _aware(value: datetime, field: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")
