import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.db.models.ledger import (
    Fill,
    FillRealizedPnL,
    KairoCapitalAuthorizationRecord,
    KairoOrder,
    OrderIntent,
    SiphonAllocation,
    SiphonEvent,
    SyntheticEvidenceManifest,
    TreasuryCashConsumption,
    TreasuryExecution,
)
from app.db.models.projections import CapitalCell, OwnershipTreasuryHolding
from app.db.models.replication import (
    CellGenesisEvent,
    CellReplicationProposal,
    ReplicationCashConsumption,
    ReplicationProposalReservation,
    ReplicationReservationEvent,
)
from app.db.models.risk import RiskGovernorState, RiskSession
from engine.execution.virtual_clock import ReplayIdentityFactory, VirtualClock
from engine.risk.models import OperationalState, RiskSessionSpec, TransitionReason
from engine.risk.state_machine import RiskStateMachine
from engine.treasury.treasury_manager import TreasuryManager


CENT = Decimal("0.01")


def _money(value: Decimal | int | str) -> str:
    return f"{Decimal(value).quantize(CENT):.2f}"


def _floor_cent(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_DOWN)


class FlywheelEvidenceManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest_version: str = "PHASE-4-FLYWHEEL-v1"
    run_timestamp: datetime
    a001_final_state: dict[str, Any]
    a002_final_state: dict[str, Any]
    siphon_reconciliation: dict[str, Any]
    treasury_execution_manifest: dict[str, Any]
    genesis_manifest_hash: str
    conservation_proof_passed: bool
    manifest_hash: str = ""

    @model_validator(mode="after")
    def valid_manifest(self) -> "FlywheelEvidenceManifest":
        if self.run_timestamp.tzinfo is None or self.run_timestamp.utcoffset() is None:
            raise ValueError("run_timestamp must be timezone-aware")
        if self.manifest_hash and self.manifest_hash != self.compute_sha256_hash():
            raise ValueError("manifest_hash does not match canonical flywheel payload")
        return self

    def compute_sha256_hash(self) -> str:
        payload = {
            "a001_final_state": self.a001_final_state,
            "a002_final_state": self.a002_final_state,
            "conservation_proof_passed": self.conservation_proof_passed,
            "genesis_manifest_hash": self.genesis_manifest_hash,
            "manifest_version": self.manifest_version,
            "run_timestamp": self.run_timestamp.isoformat(),
            "siphon_reconciliation": self.siphon_reconciliation,
            "treasury_execution_manifest": self.treasury_execution_manifest,
        }
        serialized = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    def with_hash(self) -> "FlywheelEvidenceManifest":
        return self.model_copy(update={"manifest_hash": self.compute_sha256_hash()})


class FlywheelRunner:
    """Single-threaded causal coordinator and Phase 4 conservation auditor.

    Feed items are synchronous dictionaries containing session/timestamp fields and
    optional ``cell_engines``. A cell engine exposes ``step(timestamp,
    market_slice, can_trade=bool)``. The runner owns lifecycle ordering and ARM
    authority; the delegated engine retains strategy, risk, and execution logic.
    """

    def __init__(
        self, db_session: Session, initial_seed_usd: Decimal = Decimal("100.00")
    ) -> None:
        self.db = db_session
        self.initial_seed_usd = initial_seed_usd
        self.processing_trace: list[tuple[datetime, str]] = []
        self._session_cells: dict[str, tuple[CapitalCell, ...]] = {}

    def run_replay(
        self, market_data_feed: list[dict[str, Any]]
    ) -> FlywheelEvidenceManifest:
        if not market_data_feed:
            raise ValueError("flywheel replay requires at least one market timestamp")
        self.processing_trace.clear()
        self._session_cells.clear()
        previous_timestamp: datetime | None = None
        for market_slice in market_data_feed:
            timestamp = self._aware(market_slice.get("timestamp"), "timestamp")
            session_open = self._aware(market_slice.get("session_open"), "session_open")
            session_close = self._aware(market_slice.get("session_close"), "session_close")
            session_id = market_slice.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("each market slice requires session_id")
            if not session_open <= timestamp <= session_close:
                raise ValueError("market timestamp falls outside its session window")
            if previous_timestamp is not None and timestamp < previous_timestamp:
                raise ValueError("flywheel market timestamps must be chronological")
            previous_timestamp = timestamp

            if session_id not in self._session_cells:
                self._session_cells[session_id] = self._initialize_session(
                    session_id=session_id,
                    session_open=session_open,
                    session_close=session_close,
                    arm_cells=set(market_slice.get("arm_cells", ())),
                )
            engines = market_slice.get("cell_engines", {})
            if not isinstance(engines, dict):
                raise ValueError("cell_engines must be a mapping keyed by cell_code")
            payload = market_slice.get("market_slice", market_slice.get("data", {}))
            for cell in self._session_cells[session_id]:
                self.processing_trace.append((timestamp, cell.cell_code))
                engine = engines.get(cell.cell_code)
                if engine is None:
                    continue
                state = self.db.get(RiskGovernorState, cell.cell_id)
                can_trade = state is not None and state.operational_state == "ARMED"
                engine.step(timestamp, payload, can_trade=can_trade)

        cell_ids = [row.cell_id for row in self._canonical_cells()]
        for cell_id in cell_ids:
            if self.db.scalar(
                select(func.count()).select_from(TreasuryExecution).where(
                    TreasuryExecution.cell_id == cell_id,
                    TreasuryExecution.is_synthetic.is_(True),
                )
            ):
                TreasuryManager(self.db).rebuild_holdings_projection(
                    cell_id=cell_id, is_synthetic=True
                )

        reconciliation = self._siphon_reconciliation()
        treasury_manifest = self._treasury_manifest()
        manifest = FlywheelEvidenceManifest(
            run_timestamp=previous_timestamp,
            a001_final_state=self._cell_final_state("A001"),
            a002_final_state=self._cell_final_state("A002"),
            siphon_reconciliation=reconciliation,
            treasury_execution_manifest=treasury_manifest,
            genesis_manifest_hash=self._genesis_manifest_hash(),
            conservation_proof_passed=(
                reconciliation["conservation_proof_passed"]
                and self._has_zero_orphans()
                and treasury_manifest["projection_equivalent"]
            ),
        ).with_hash()
        self._persist_manifest(manifest)
        return manifest

    def _initialize_session(
        self,
        *,
        session_id: str,
        session_open: datetime,
        session_close: datetime,
        arm_cells: set[str],
    ) -> tuple[CapitalCell, ...]:
        cells = self._canonical_cells()
        eligible: list[CapitalCell] = []
        for cell in cells:
            genesis = self.db.scalar(
                select(CellGenesisEvent).where(CellGenesisEvent.child_cell_id == cell.cell_id)
            )
            # A child born at any point in this session is quarantined for the
            # entire session, including timestamps after genesis.
            if genesis is not None and genesis.occurred_at >= session_open:
                continue
            risk_session_id = f"{session_id}:{cell.cell_code}"
            existing_session = self.db.get(RiskSession, risk_session_id)
            existing_state = self.db.get(RiskGovernorState, cell.cell_id)
            # A repeated audit of already-persisted replay facts must not rewind
            # the cell or emit duplicate lifecycle facts.
            if (
                existing_session is not None
                and existing_state is not None
                and existing_state.current_session_id != risk_session_id
            ):
                current = self.db.get(RiskSession, existing_state.current_session_id)
                if current is not None and current.session_open > session_open:
                    eligible.append(cell)
                    continue
            clock = VirtualClock(session_open)
            state_machine = RiskStateMachine(
                self.db,
                cell_id=cell.cell_id,
                clock=clock,
                identities=ReplayIdentityFactory(risk_session_id),
            )
            state = state_machine.initialize_session(RiskSessionSpec(
                session_id=risk_session_id,
                trading_date=session_open.date(),
                session_open=session_open,
                session_close=session_close,
            ))
            if state.session_net_pnl != 0 and cell.status == "INITIALIZING":
                raise ValueError("new child risk state must begin at zero P&L")
            if cell.status == "INITIALIZING":
                cell.status = "ACTIVE"
                cell.updated_at = session_open
            if cell.cell_code in arm_cells:
                authorization = self.db.scalar(
                    select(KairoCapitalAuthorizationRecord)
                    .where(KairoCapitalAuthorizationRecord.cell_id == cell.cell_id)
                    .order_by(KairoCapitalAuthorizationRecord.computed_at.desc())
                    .limit(1)
                )
                if authorization is None:
                    raise ValueError(f"cell {cell.cell_code} has no capital authorization")
                if state.operational_state == "DISARMED":
                    clock.advance_to(session_open + timedelta(microseconds=1))
                    state_machine.transition(
                        state,
                        OperationalState.ARMED,
                        TransitionReason.MANUAL_ARM,
                        authorization.authorized_trading_cash,
                    )
            eligible.append(cell)
        self.db.flush()
        return tuple(eligible)

    def _canonical_cells(self) -> list[CapitalCell]:
        return list(self.db.scalars(
            select(CapitalCell)
            .where(
                CapitalCell.economic_domain == "SYNTHETIC",
                CapitalCell.status.in_(("INITIALIZING", "ACTIVE", "PAUSED", "HALTED_FOR_DAY")),
            )
            .order_by(CapitalCell.cell_code)
        ))

    def _siphon_reconciliation(self) -> dict[str, Any]:
        per_cell: dict[str, Any] = {}
        global_values = {
            "qualified_profit_usd": Decimal("0"),
            "net_settled_realized_pnl_usd": Decimal("0"),
            "safety_allocated_usd": Decimal("0"),
            "treasury_allocated_usd": Decimal("0"),
            "replication_allocated_usd": Decimal("0"),
        }
        passed = True
        for cell in self._canonical_cells():
            siphons = list(self.db.scalars(
                select(SiphonEvent).where(
                    SiphonEvent.cell_id == cell.cell_id,
                    SiphonEvent.is_synthetic.is_(True),
                    SiphonEvent.policy_id != "LEGACY-SIPHON-v0",
                ).order_by(SiphonEvent.occurred_at, SiphonEvent.siphon_id)
            ))
            siphon_ids = [row.siphon_id for row in siphons]
            allocations = list(self.db.scalars(
                select(SiphonAllocation).where(SiphonAllocation.siphon_id.in_(siphon_ids))
            )) if siphon_ids else []
            bucket_totals = {
                bucket: sum(
                    (Decimal(row.allocated_usd) for row in allocations if row.bucket_type == bucket),
                    Decimal("0"),
                )
                for bucket in ("SAFETY_RESERVE", "TARGET_TREASURY", "REPLICATION_POOL")
            }
            qualified = sum((Decimal(row.qualified_profit_usd) for row in siphons), Decimal("0"))
            exact_events = all(
                Decimal(row.qualified_profit_usd)
                == Decimal(row.safety_reserve_usd)
                + Decimal(row.target_treasury_usd)
                + Decimal(row.replication_pool_usd)
                and Decimal(row.safety_reserve_usd)
                == _floor_cent(Decimal(row.qualified_profit_usd) * Decimal("0.40"))
                and Decimal(row.target_treasury_usd)
                == _floor_cent(Decimal(row.qualified_profit_usd) * Decimal("0.40"))
                for row in siphons
            )
            pnl_rows = list(self.db.execute(
                select(FillRealizedPnL.realized_pnl_usd, Fill.commission_fee_usd)
                .join(Fill, Fill.fill_id == FillRealizedPnL.fill_id)
                .where(
                    FillRealizedPnL.cell_id == cell.cell_id,
                    Fill.is_simulated.is_(True),
                )
            ))
            net_pnl = sum(
                (Decimal(pnl) - Decimal(fee) for pnl, fee in pnl_rows), Decimal("0")
            )
            treasury_consumed = Decimal(self.db.scalar(
                select(func.coalesce(func.sum(TreasuryCashConsumption.consumed_usd), 0))
                .join(SiphonAllocation, SiphonAllocation.allocation_id == TreasuryCashConsumption.allocation_id)
                .join(SiphonEvent, SiphonEvent.siphon_id == SiphonAllocation.siphon_id)
                .where(
                    SiphonEvent.cell_id == cell.cell_id,
                    SiphonEvent.is_synthetic.is_(True),
                )
            ) or 0)
            replication_consumed = Decimal(self.db.scalar(
                select(func.coalesce(func.sum(ReplicationCashConsumption.consumed_usd), 0))
                .join(CellReplicationProposal, CellReplicationProposal.proposal_id == ReplicationCashConsumption.proposal_id)
                .where(CellReplicationProposal.parent_cell_id == cell.cell_id)
            ) or 0)
            latest_reservation = (
                select(ReplicationReservationEvent.event_type)
                .where(
                    ReplicationReservationEvent.reservation_id
                    == ReplicationProposalReservation.reservation_id
                )
                .order_by(
                    ReplicationReservationEvent.occurred_at.desc(),
                    ReplicationReservationEvent.event_id.desc(),
                )
                .limit(1)
                .correlate(ReplicationProposalReservation)
                .scalar_subquery()
            )
            active_reserved = Decimal(self.db.scalar(
                select(func.coalesce(func.sum(ReplicationProposalReservation.reserved_usd), 0))
                .join(CellReplicationProposal)
                .where(
                    CellReplicationProposal.parent_cell_id == cell.cell_id,
                    latest_reservation == "RESERVED",
                )
            ) or 0)
            treasury_unconsumed = bucket_totals["TARGET_TREASURY"] - treasury_consumed
            replication_uncommitted = (
                bucket_totals["REPLICATION_POOL"] - replication_consumed - active_reserved
            )
            cell_passed = (
                exact_events
                and qualified == sum(bucket_totals.values(), Decimal("0"))
                and qualified <= net_pnl
                and treasury_unconsumed >= 0
                and replication_uncommitted >= 0
            )
            passed = passed and cell_passed
            per_cell[cell.cell_code] = {
                "qualified_profit_usd": _money(qualified),
                "net_settled_realized_pnl_usd": _money(net_pnl),
                "safety_allocated_usd": _money(bucket_totals["SAFETY_RESERVE"]),
                "safety_reserve_balance_usd": _money(bucket_totals["SAFETY_RESERVE"]),
                "treasury_allocated_usd": _money(bucket_totals["TARGET_TREASURY"]),
                "treasury_consumed_usd": _money(treasury_consumed),
                "treasury_unconsumed_usd": _money(treasury_unconsumed),
                "replication_allocated_usd": _money(bucket_totals["REPLICATION_POOL"]),
                "replication_consumed_usd": _money(replication_consumed),
                "replication_active_reserved_usd": _money(active_reserved),
                "replication_uncommitted_usd": _money(replication_uncommitted),
                "conservation_passed": cell_passed,
            }
            for key in global_values:
                global_values[key] += {
                    "qualified_profit_usd": qualified,
                    "net_settled_realized_pnl_usd": net_pnl,
                    "safety_allocated_usd": bucket_totals["SAFETY_RESERVE"],
                    "treasury_allocated_usd": bucket_totals["TARGET_TREASURY"],
                    "replication_allocated_usd": bucket_totals["REPLICATION_POOL"],
                }[key]
        return {
            "cells": per_cell,
            "global": {key: _money(value) for key, value in global_values.items()},
            "conservation_proof_passed": passed,
        }

    def _treasury_manifest(self) -> dict[str, Any]:
        executions: list[dict[str, Any]] = []
        projection_equivalent = True
        for cell in self._canonical_cells():
            rows = list(self.db.scalars(
                select(TreasuryExecution).where(
                    TreasuryExecution.cell_id == cell.cell_id,
                    TreasuryExecution.is_synthetic.is_(True),
                ).order_by(
                    TreasuryExecution.occurred_at,
                    TreasuryExecution.instrument_id,
                    TreasuryExecution.execution_id,
                )
            ))
            for row in rows:
                executions.append({
                    "cell_code": cell.cell_code,
                    "target_config_id": str(row.target_config_id),
                    "instrument_id": str(row.instrument_id),
                    "symbol": row.symbol,
                    "shares_executed": str(row.shares_executed),
                    "execution_price_usd": str(row.execution_price_usd),
                    "gross_amount_usd": _money(row.gross_amount_usd),
                    "fee_usd": _money(row.fee_usd),
                    "net_amount_usd": _money(row.net_amount_usd),
                    "occurred_at": row.occurred_at.isoformat(),
                })
            grouped: dict[UUID, tuple[Decimal, Decimal]] = {}
            for row in rows:
                shares, basis = grouped.get(row.instrument_id, (Decimal("0"), Decimal("0")))
                grouped[row.instrument_id] = (
                    shares + Decimal(row.shares_executed),
                    basis + Decimal(row.net_amount_usd),
                )
            holdings = list(self.db.scalars(select(OwnershipTreasuryHolding).where(
                OwnershipTreasuryHolding.cell_id == cell.cell_id,
                OwnershipTreasuryHolding.is_synthetic.is_(True),
            )))
            actual = {
                row.instrument_id: (Decimal(row.total_shares), Decimal(row.cumulative_cost_basis_usd))
                for row in holdings
            }
            projection_equivalent = projection_equivalent and grouped == actual
        return {
            "executions": executions,
            "projection_equivalent": projection_equivalent,
        }

    def _cell_final_state(self, cell_code: str) -> dict[str, Any]:
        cell = self.db.scalar(select(CapitalCell).where(CapitalCell.cell_code == cell_code))
        if cell is None:
            return {}
        state = self.db.get(RiskGovernorState, cell.cell_id)
        risk_session = self.db.get(RiskSession, state.current_session_id) if state else None
        return {
            "cell_id": str(cell.cell_id),
            "cell_code": cell.cell_code,
            "status": cell.status,
            "economic_domain": cell.economic_domain,
            "risk_session_id": risk_session.session_id if risk_session else None,
            "operational_state": state.operational_state if state else None,
            "session_realized_pnl_usd": _money(state.session_realized_pnl if state else 0),
            "session_unrealized_pnl_usd": _money(state.session_unrealized_pnl if state else 0),
            "session_net_pnl_usd": _money(state.session_net_pnl if state else 0),
        }

    def _genesis_manifest_hash(self) -> str:
        child = self.db.scalar(select(CapitalCell).where(CapitalCell.cell_code == "A002"))
        if child is None:
            return ""
        row = self.db.scalar(
            select(SyntheticEvidenceManifest)
            .where(
                SyntheticEvidenceManifest.cell_id == child.cell_id,
                SyntheticEvidenceManifest.manifest_type == "GENESIS_SEED",
            )
            .order_by(SyntheticEvidenceManifest.created_at, SyntheticEvidenceManifest.manifest_id)
            .limit(1)
        )
        return row.manifest_hash.strip() if row else ""

    def _has_zero_orphans(self) -> bool:
        orphan_orders = self.db.scalar(
            select(func.count()).select_from(KairoOrder).outerjoin(
                OrderIntent, OrderIntent.intent_id == KairoOrder.intent_id
            ).where(OrderIntent.intent_id.is_(None))
        )
        orphan_fills = self.db.scalar(
            select(func.count()).select_from(Fill).outerjoin(
                KairoOrder, KairoOrder.kairo_order_id == Fill.kairo_order_id
            ).where(KairoOrder.kairo_order_id.is_(None))
        )
        unmatched_consumptions = self.db.scalar(
            select(func.count()).select_from(ReplicationCashConsumption).outerjoin(
                ReplicationProposalReservation,
                and_(
                    ReplicationProposalReservation.proposal_id
                    == ReplicationCashConsumption.proposal_id,
                    ReplicationProposalReservation.allocation_id
                    == ReplicationCashConsumption.allocation_id,
                ),
            ).where(ReplicationProposalReservation.reservation_id.is_(None))
        )
        reservation_without_event = self.db.scalar(
            select(func.count()).select_from(ReplicationProposalReservation).where(
                ~select(ReplicationReservationEvent.event_id).where(
                    ReplicationReservationEvent.reservation_id
                    == ReplicationProposalReservation.reservation_id
                ).exists()
            )
        )
        return not any((orphan_orders, orphan_fills, unmatched_consumptions, reservation_without_event))

    def _persist_manifest(self, manifest: FlywheelEvidenceManifest) -> None:
        root = self.db.scalar(select(CapitalCell).where(CapitalCell.cell_code == "A001"))
        if root is None:
            raise ValueError("A001 is required as the aggregate flywheel evidence authority")
        manifest_id = uuid5(
            NAMESPACE_URL, f"kairo:phase4-flywheel:{root.cell_id}:{manifest.manifest_hash}"
        )
        source_refs = {
            "a001_cell_id": manifest.a001_final_state.get("cell_id"),
            "a002_cell_id": manifest.a002_final_state.get("cell_id"),
            "genesis_manifest_hash": manifest.genesis_manifest_hash,
            "flywheel_manifest_hash": manifest.manifest_hash,
        }
        values = {
            "manifest_type": "FLYWHEEL_REPLAY",
            "manifest_hash": manifest.manifest_hash,
            "manifest_algorithm": manifest.manifest_version,
            "cell_id": root.cell_id,
            "source_count": 3,
            "source_refs": source_refs,
            "model_identifier": "KAIRO-FLYWHEEL",
            "model_version": "1.0.0",
            "created_at": manifest.run_timestamp,
        }
        existing = self.db.get(SyntheticEvidenceManifest, manifest_id)
        if existing is None:
            self.db.add(SyntheticEvidenceManifest(manifest_id=manifest_id, **values))
            self.db.flush()
        elif any(getattr(existing, key) != value for key, value in values.items()):
            raise ValueError("deterministic flywheel manifest identity conflict")

    @staticmethod
    def _aware(value: Any, field_name: str) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be a timezone-aware datetime")
        return value
