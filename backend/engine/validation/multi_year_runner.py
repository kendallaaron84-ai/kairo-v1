import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models.broker import BrokerAccount
from app.db.models.configuration import CellTreasuryConfig, RiskPolicy, StrategyRegistry
from app.db.models.historical import HistoricalMarketDataset, HistoricalMarketDatasetSymbol
from app.db.models.ledger import Fill, FillRealizedPnL, KairoCapitalAuthorizationRecord, MarketSnapshot, OrderIntent, SiphonAllocation, SiphonEvent, SyntheticEvidenceManifest, TreasuryExecution
from app.db.models.projections import CapitalCell
from app.db.models.replication import CellGenesisEvent, ReplicationAuthorization
from app.db.models.risk import RiskGovernorState
from engine.execution.replay_orchestrator import ReplayOptionChainEvent, ReplayOrchestrator, ReplayRunResult, ReplaySessionConfig, ResearchReplayInput
from engine.replication.genesis_factory import GenesisFactory
from engine.replication.models import AuthorizationDecision
from engine.replication.replication_manager import ReplicationManager
from engine.replication.services.human_authorization_service import HumanAuthorizationService
from engine.siphon.models import SyntheticSettlementMetadata
from engine.siphon.siphon_manager import SiphonManager
from engine.treasury.treasury_manager import TreasuryManager
from engine.validation.models import StreamRole
from engine.validation.session_calendar import SessionCalendarResolver
from engine.validation.stream_loader import HistoricalDatasetStreamLoader, LoadedHistoricalStream


REPLAY_AUTHORITY = "REPLAY_SIMULATED_HUMAN_AUTHORIZATION"


class MultiYearReplayConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    dataset_id: UUID
    cell_id: UUID
    broker_account_id: UUID
    start_date: date
    end_date: date
    strategy_id: str = "EMA-CROSS-001"
    strategy_version: str = "1.0.0"
    strategy_parameters_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_versions: dict[str, str]

    @model_validator(mode="after")
    def valid_window(self) -> "MultiYearReplayConfig":
        if self.end_date < self.start_date:
            raise ValueError("multi-year end date cannot precede start date")
        return self


class MultiYearReplayEvidenceManifest(BaseModel):
    model_config = ConfigDict(frozen=True)
    manifest_version: str = "MULTI-YEAR-REPLAY-MANIFEST-v1"
    payload: dict[str, Any]
    multi_year_manifest_sha256: str

    @classmethod
    def build(cls, payload: dict[str, Any]) -> "MultiYearReplayEvidenceManifest":
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        return cls(payload=payload, multi_year_manifest_sha256=digest)


class MultiYearReplayResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    sessions_processed: int
    session_manifest_hashes: tuple[str, ...]
    missing_option_bars: int
    skipped_executions: int
    evidence_manifest: MultiYearReplayEvidenceManifest


class CanonicalPostSessionLifecycle:
    """Sequences existing canonical capital lifecycle services without accounting math."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def finalize(self, *, cell_id: UUID, session_id: str, occurred_at: datetime) -> None:
        authorization = self.session.scalar(select(KairoCapitalAuthorizationRecord).where(
            KairoCapitalAuthorizationRecord.cell_id == cell_id,
            KairoCapitalAuthorizationRecord.economic_domain == "SYNTHETIC",
        ).order_by(KairoCapitalAuthorizationRecord.computed_at.desc()).limit(1))
        if authorization is None:
            raise ValueError("synthetic replay cell lacks canonical capital authorization")
        identity = lambda record_type, stable_key: uuid5(NAMESPACE_URL, f"kairo:multi-year:{session_id}:{record_type}:{stable_key}")
        SiphonManager(self.session, identity_factory=identity).qualify_and_allocate(
            cell_id=cell_id, occurred_at=occurred_at,
            synthetic_settled_cash_usd=Decimal(authorization.settled_cash),
            synthetic_settlement_metadata=SyntheticSettlementMetadata(
                synthetic_settled_at=occurred_at, replay_session_id=session_id,
                model_version="SETTLEMENT-SIM-v0.1",
            ),
        )
        target = self.session.scalar(select(CellTreasuryConfig).where(
            CellTreasuryConfig.cell_id == cell_id, CellTreasuryConfig.is_active.is_(True)
        ))
        if target is not None:
            snapshot = self.session.scalar(select(MarketSnapshot).where(
                MarketSnapshot.instrument_id == target.target_instrument_id,
                MarketSnapshot.captured_at <= occurred_at,
            ).order_by(MarketSnapshot.captured_at.desc(), MarketSnapshot.snapshot_id.desc()).limit(1))
            if snapshot is not None:
                TreasuryManager(self.session, identity_factory=identity).execute_available(
                    cell_id=cell_id, is_synthetic=True,
                    market_snapshot_ids={target.config_id: snapshot.snapshot_id}, occurred_at=occurred_at,
                )
        proposal = ReplicationManager(self.session).create_proposal(
            parent_cell_id=cell_id, proposed_child_code=self._next_child_code(),
            is_synthetic=True, occurred_at=occurred_at,
        )
        if proposal is not None:
            auth = HumanAuthorizationService(self.session).authorize(
                proposal_id=proposal.proposal_id, manifest_hash=proposal.manifest_hash,
                decision=AuthorizationDecision.APPROVE, authorized_by=REPLAY_AUTHORITY,
                authorization_method="DETERMINISTIC_REPLAY", authorized_at=occurred_at,
            )
            if auth.authorized_by != REPLAY_AUTHORITY:
                raise ValueError("historical Genesis authorization provenance was not preserved")
            GenesisFactory(self.session).instantiate_child_cell(proposal_id=proposal.proposal_id, occurred_at=occurred_at)

    def _next_child_code(self) -> str:
        codes = list(self.session.scalars(select(CapitalCell.cell_code).where(CapitalCell.cell_code.like("A%"))))
        numbers = [int(code[1:]) for code in codes if code[1:].isdigit()]
        return f"A{max(numbers, default=0) + 1:03d}"


class MultiYearReplayRunner:
    """Synchronous conductor over canonical replay and post-session authorities."""

    def __init__(self, session: Session, config: MultiYearReplayConfig, *,
                 stream_loader: HistoricalDatasetStreamLoader | None = None,
                 calendar: SessionCalendarResolver | None = None,
                 lifecycle: CanonicalPostSessionLifecycle | None = None,
                 orchestrator_factory: Callable[[Session, ReplaySessionConfig], ReplayOrchestrator] = ReplayOrchestrator) -> None:
        self.session = session
        self.config = config
        self.loader = stream_loader or HistoricalDatasetStreamLoader(session)
        self.calendar = calendar or SessionCalendarResolver()
        self.lifecycle = lifecycle or CanonicalPostSessionLifecycle(session)
        self.orchestrator_factory = orchestrator_factory
        self.persisted_state_trace: list[dict[str, Any]] = []

    def run(self) -> MultiYearReplayResult:
        dataset = self.session.get(HistoricalMarketDataset, self.config.dataset_id)
        cell = self.session.get(CapitalCell, self.config.cell_id)
        broker = self.session.get(BrokerAccount, self.config.broker_account_id)
        strategy = self.session.get(StrategyRegistry, (self.config.strategy_id, self.config.strategy_version))
        if dataset is None or cell is None or strategy is None:
            raise ValueError("dataset, cell, and strategy identities must resolve canonically")
        if cell.economic_domain != "SYNTHETIC" or broker is None or broker.environment != "PAPER":
            raise ValueError("multi-year replay requires a synthetic cell and PAPER broker")
        risk = self.session.get(RiskPolicy, cell.risk_policy_id)
        if risk is None:
            raise ValueError("cell risk policy does not resolve")
        streams = self.loader.load(dataset.dataset_id)
        session_hashes: list[str] = []
        missing = 0
        events_processed = 0
        sessions_processed = 0
        for trading_date, opened, closed in self.calendar.sessions(self.config.start_date, self.config.end_date):
            session_streams = self._session_streams(streams, opened, closed)
            if not session_streams:
                continue
            authority = self._persisted_authority(cell.cell_id)
            session_id = f"MYR:{dataset.dataset_manifest_sha256[:16]}:{cell.cell_code}:{trading_date.isoformat()}"
            orchestrator = self.orchestrator_factory(self.session, ReplaySessionConfig(
                session_id=session_id, cell_id=cell.cell_id, broker_account_id=broker.broker_account_id,
                session_open=opened, session_close=closed, execution_authorized_for_replay=True,
                strategy_id=strategy.strategy_id, strategy_version=strategy.version_tag,
                initial_cash_usd=Decimal(authority.authorized_trading_cash),
            ))
            result = orchestrator.replay_research(session_streams)
            session_hashes.append(result.manifest_hash)
            events_processed += result.event_count
            missing += len(result.missing_execution_evidence)
            self.lifecycle.finalize(cell_id=cell.cell_id, session_id=session_id, occurred_at=closed)
            self.session.flush(); self.session.expire_all()
            sessions_processed += 1
        payload = self._manifest_payload(dataset, streams, cell, strategy, risk, sessions_processed, session_hashes, missing, events_processed)
        manifest = MultiYearReplayEvidenceManifest.build(payload)
        self._persist_manifest(manifest, cell.cell_id)
        return MultiYearReplayResult(
            sessions_processed=sessions_processed, session_manifest_hashes=tuple(session_hashes),
            missing_option_bars=missing, skipped_executions=missing, evidence_manifest=manifest,
        )

    def _session_streams(self, streams: tuple[LoadedHistoricalStream, ...], opened: datetime, closed: datetime) -> tuple[ResearchReplayInput, ...]:
        chain_rows = [row for row in streams if row.stream_role is StreamRole.OPTION_CHAIN_QUOTES]
        results: list[ResearchReplayInput] = []
        for row in streams:  # persisted ordinal order from loader is the only ordering authority
            if row.stream_role is not StreamRole.UNDERLYING_SIGNAL_BARS or row.replay_input is None:
                continue
            events = tuple(item for item in row.replay_input.events if opened <= item.timestamp <= closed)
            if not events:
                continue
            chains: list[ReplayOptionChainEvent] = []
            for chain_row in chain_rows:
                chains.extend(item for item in chain_row.option_chains if item.underlying_instrument_id == row.instrument_id and opened <= item.timestamp <= closed)
            chains.sort(key=lambda item: item.timestamp)
            if len({item.timestamp for item in chains}) != len(chains):
                raise ValueError("multiple option streams claim the same underlying timestamp")
            results.append(ResearchReplayInput(provider=row.replay_input.provider, events=events, option_chains=tuple(chains)))
        return tuple(results)

    def _persisted_authority(self, cell_id: UUID) -> KairoCapitalAuthorizationRecord:
        authorization = self.session.scalar(select(KairoCapitalAuthorizationRecord).where(
            KairoCapitalAuthorizationRecord.cell_id == cell_id,
            KairoCapitalAuthorizationRecord.economic_domain == "SYNTHETIC",
        ).order_by(KairoCapitalAuthorizationRecord.computed_at.desc()).limit(1))
        state = self.session.get(RiskGovernorState, cell_id)
        if authorization is None:
            raise ValueError("cell has no persisted synthetic capital authority")
        self.persisted_state_trace.append({
            "authorization_id": str(authorization.authorization_id),
            "authorized_trading_cash": str(authorization.authorized_trading_cash),
            "risk_session_id": state.current_session_id if state else None,
            "operational_state": state.operational_state if state else None,
        })
        return authorization

    def _manifest_payload(self, dataset, streams, cell, strategy, risk, session_count, session_hashes, missing, events_processed) -> dict[str, Any]:
        normalized = {f"{row.stream_ordinal}:{row.symbol}": row.normalized_content_sha256 for row in streams}
        fills = self.session.scalar(select(func.count()).select_from(Fill).where(Fill.is_simulated.is_(True))) or 0
        intents = self.session.scalar(select(func.count()).select_from(OrderIntent).where(OrderIntent.cell_id == cell.cell_id)) or 0
        siphons = list(self.session.scalars(select(SiphonEvent).where(SiphonEvent.cell_id == cell.cell_id, SiphonEvent.is_synthetic.is_(True))))
        allocations = list(self.session.scalars(select(SiphonAllocation).where(SiphonAllocation.siphon_id.in_([row.siphon_id for row in siphons])))) if siphons else []
        genesis = list(self.session.scalars(select(CellGenesisEvent).where(CellGenesisEvent.parent_cell_id == cell.cell_id).order_by(CellGenesisEvent.occurred_at, CellGenesisEvent.genesis_id)))
        genesis_auth = list(self.session.scalars(select(ReplicationAuthorization).where(ReplicationAuthorization.authorized_by == REPLAY_AUTHORITY).order_by(ReplicationAuthorization.authorized_at, ReplicationAuthorization.authorization_id)))
        pnl_rows = list(self.session.execute(select(FillRealizedPnL.realized_pnl_usd, Fill.commission_fee_usd).join(Fill, Fill.fill_id == FillRealizedPnL.fill_id).where(FillRealizedPnL.cell_id == cell.cell_id, Fill.is_simulated.is_(True))))
        net_pnl = sum((Decimal(pnl) - Decimal(fee) for pnl, fee in pnl_rows), Decimal("0"))
        return {
            "dataset_manifest_sha256": dataset.dataset_manifest_sha256,
            "normalized_artifact_hashes": dict(sorted(normalized.items())),
            "calendar_authority": {"name": dataset.calendar_name, "version": dataset.calendar_version},
            "normalization_policy_version": dataset.normalization_policy_version,
            "strategy_identity": {"strategy_id": strategy.strategy_id, "version": strategy.version_tag, "parameters_hash": self.config.strategy_parameters_sha256},
            "risk_policy_identity": {"policy_id": str(risk.policy_id), "version": risk.policy_identifier, "loss_floor": str(risk.daily_loss_floor_usd)},
            "initial_cell_topology": {"cell_code": cell.cell_code, "starting_capital": str(cell.seed_capital)},
            "sample_window": {"start_date": self.config.start_date.isoformat(), "end_date": self.config.end_date.isoformat(), "sessions_count": session_count},
            "execution_metrics": {"events_processed": events_processed, "trade_count": int(intents), "fill_count": int(fills), "session_manifest_hashes": session_hashes},
            "financial_reconciliation": {
                "net_pnl": str(net_pnl),
                "siphoned_safety": str(sum((Decimal(row.allocated_usd) for row in allocations if row.bucket_type == "SAFETY_RESERVE"), Decimal("0"))),
                "siphoned_treasury": str(sum((Decimal(row.allocated_usd) for row in allocations if row.bucket_type == "TARGET_TREASURY"), Decimal("0"))),
                "siphoned_rep": str(sum((Decimal(row.allocated_usd) for row in allocations if row.bucket_type == "REPLICATION_POOL"), Decimal("0"))),
                "treasury_execution_count": int(self.session.scalar(select(func.count()).select_from(TreasuryExecution).where(TreasuryExecution.cell_id == cell.cell_id)) or 0),
            },
            "missing_evidence_metrics": {"missing_option_bars": missing, "skipped_executions": missing},
            "genesis_outcomes": {"authority_mode": REPLAY_AUTHORITY, "cells_spawned": len(genesis), "genesis_events": [str(row.genesis_id) for row in genesis], "authorization_ids": [str(row.authorization_id) for row in genesis_auth]},
            "engine_versions": dict(sorted(self.config.engine_versions.items())),
        }

    def _persist_manifest(self, manifest: MultiYearReplayEvidenceManifest, cell_id: UUID) -> None:
        manifest_id = uuid5(NAMESPACE_URL, f"kairo:multi-year-replay:{cell_id}:{manifest.multi_year_manifest_sha256}")
        existing = self.session.get(SyntheticEvidenceManifest, manifest_id)
        if existing is None:
            self.session.add(SyntheticEvidenceManifest(
                manifest_id=manifest_id, manifest_type="MULTI_YEAR_REPLAY",
                manifest_hash=manifest.multi_year_manifest_sha256,
                manifest_algorithm=manifest.manifest_version, cell_id=cell_id,
                source_count=len(manifest.payload["normalized_artifact_hashes"]),
                source_refs=manifest.payload, model_identifier=self.config.strategy_id,
                model_version=self.config.strategy_version,
                created_at=datetime.combine(self.config.end_date, datetime.min.time(), timezone.utc),
            )); self.session.flush()
