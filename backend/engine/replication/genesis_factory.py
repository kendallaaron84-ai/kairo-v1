from datetime import datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.configuration import CellTreasuryConfig, Instrument, RiskPolicy
from app.db.models.ledger import KairoCapitalAuthorizationRecord, SiphonAllocation, SyntheticEvidenceManifest
from app.db.models.projections import CapitalCell
from app.db.models.replication import (
    CellGenesisEvent,
    CellReplicationProposal,
    ReplicationAuthorization,
    ReplicationCashConsumption,
    ReplicationProposalEvent,
    ReplicationProposalReservation,
    ReplicationReservationEvent,
)
from engine.replication.models import GenesisAllocationSource, GenesisSeedManifest


class GenesisFactory:
    """Atomic, deterministic Step 3B child-cell instantiation authority."""

    SEED = Decimal("100.00")

    def __init__(self, session: Session) -> None:
        self.session = session

    def instantiate_child_cell(
        self, *, proposal_id: UUID, occurred_at: datetime
    ) -> CapitalCell:
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        with self.session.begin_nested():
            return self._instantiate(proposal_id=proposal_id, occurred_at=occurred_at)

    def _instantiate(self, *, proposal_id: UUID, occurred_at: datetime) -> CapitalCell:
        proposal = self.session.scalar(
            select(CellReplicationProposal)
            .where(CellReplicationProposal.proposal_id == proposal_id)
            .with_for_update()
        )
        if proposal is None:
            raise ValueError("replication proposal does not exist")
        existing_genesis = self.session.scalar(
            select(CellGenesisEvent).where(CellGenesisEvent.proposal_id == proposal_id)
        )
        if existing_genesis is not None:
            child = self.session.get(CapitalCell, existing_genesis.child_cell_id)
            if child is None:
                raise ValueError("genesis fact references a missing child cell")
            return child

        state = self.session.scalar(
            select(ReplicationProposalEvent.state_to)
            .where(ReplicationProposalEvent.proposal_id == proposal_id)
            .order_by(
                ReplicationProposalEvent.occurred_at.desc(),
                ReplicationProposalEvent.event_id.desc(),
            )
            .limit(1)
        )
        if state != "AUTHORIZED":
            raise ValueError(f"proposal must be AUTHORIZED before genesis: {state}")
        authorization = self.session.scalar(
            select(ReplicationAuthorization)
            .where(ReplicationAuthorization.proposal_id == proposal_id)
            .with_for_update()
        )
        if (
            authorization is None
            or authorization.decision != "APPROVE"
            or authorization.manifest_hash != proposal.manifest_hash
        ):
            raise ValueError("matching APPROVE authorization does not resolve")
        if Decimal(proposal.proposed_seed_capital_usd) != self.SEED:
            raise ValueError("Step 3B genesis requires exactly 100.00 seed capital")

        risk_policy = self.session.scalar(
            select(RiskPolicy).where(
                RiskPolicy.policy_identifier == proposal.risk_policy_identifier
            )
        )
        if risk_policy is None:
            raise ValueError("proposal risk policy does not resolve")
        target = self.session.get(Instrument, proposal.target_instrument_id)
        if target is None:
            raise ValueError("proposal-bound target instrument does not resolve")
        if target.symbol != proposal.target_symbol:
            raise ValueError("proposal-bound target instrument identity mismatch")

        reservations = list(self.session.scalars(
            select(ReplicationProposalReservation)
            .where(ReplicationProposalReservation.proposal_id == proposal_id)
            .order_by(ReplicationProposalReservation.allocation_id)
            .with_for_update()
        ))
        if not reservations:
            raise ValueError("proposal has no replication reservations")
        allocation_ids = [row.allocation_id for row in reservations]
        allocations = list(self.session.scalars(
            select(SiphonAllocation)
            .where(SiphonAllocation.allocation_id.in_(allocation_ids))
            .order_by(SiphonAllocation.allocation_id)
            .with_for_update()
        ))
        if {row.allocation_id for row in allocations} != set(allocation_ids):
            raise ValueError("one or more reserved allocations do not resolve")
        if sum((Decimal(row.reserved_usd) for row in reservations), Decimal("0")) != self.SEED:
            raise ValueError("reserved dollars do not equal the exact genesis seed")
        for reservation in reservations:
            latest = self.session.scalar(
                select(ReplicationReservationEvent.event_type)
                .where(ReplicationReservationEvent.reservation_id == reservation.reservation_id)
                .order_by(
                    ReplicationReservationEvent.occurred_at.desc(),
                    ReplicationReservationEvent.event_id.desc(),
                )
                .limit(1)
            )
            if latest != "RESERVED":
                raise ValueError("all proposal reservations must be RESERVED")

        child_id = uuid5(
            NAMESPACE_URL,
            f"kairo:genesis-child:{proposal.proposal_id}:{proposal.proposed_child_code}",
        )
        if self.session.get(CapitalCell, child_id) is not None:
            raise ValueError("deterministic child identity exists without genesis fact")
        if self.session.scalar(
            select(CapitalCell).where(CapitalCell.cell_code == proposal.proposed_child_code)
        ) is not None:
            raise ValueError("proposed child code is already in use")

        child = CapitalCell(
            cell_id=child_id,
            cell_code=proposal.proposed_child_code,
            seed_capital=self.SEED,
            status="INITIALIZING",
            autonomy_tier="APPRENTICE",
            strategy_id=proposal.strategy_identifier,
            strategy_version=proposal.strategy_version,
            target_treasury_code=proposal.target_treasury_code,
            risk_policy_id=risk_policy.policy_id,
            economic_domain="SYNTHETIC",
            updated_at=occurred_at,
        )
        self.session.add(child)
        self.session.flush()

        source_allocations = tuple(
            GenesisAllocationSource(
                allocation_id=row.allocation_id,
                reservation_id=row.reservation_id,
                reserved_usd=Decimal(row.reserved_usd),
            )
            for row in reservations
        )
        manifest = GenesisSeedManifest(
            proposal_id=proposal.proposal_id,
            proposal_manifest_hash=proposal.manifest_hash,
            authorization_id=authorization.authorization_id,
            parent_cell_id=proposal.parent_cell_id,
            child_cell_id=child_id,
            child_cell_code=child.cell_code,
            seed_capital_usd=self.SEED,
            strategy_identifier=proposal.strategy_identifier,
            strategy_version=proposal.strategy_version,
            risk_policy_identifier=proposal.risk_policy_identifier,
            target_config_id=proposal.target_config_id,
            target_type=proposal.target_type,
            target_instrument_id=proposal.target_instrument_id,
            target_symbol=proposal.target_symbol,
            target_treasury_code=proposal.target_treasury_code,
            created_at=occurred_at,
            source_allocations=source_allocations,
        )
        manifest_hash = manifest.sha256()
        manifest_id = uuid5(
            NAMESPACE_URL, f"kairo:genesis-seed:{child_id}:{manifest_hash}"
        )
        self.session.add(SyntheticEvidenceManifest(
            manifest_id=manifest_id,
            manifest_type=manifest.manifest_type,
            manifest_hash=manifest_hash,
            manifest_algorithm=manifest.manifest_algorithm,
            cell_id=child_id,
            source_count=2 + len(source_allocations),
            source_refs=manifest.source_refs(),
            model_identifier=manifest.model_identifier,
            model_version=manifest.model_version,
            created_at=occurred_at,
        ))
        self.session.flush()

        self.session.add(KairoCapitalAuthorizationRecord(
            authorization_id=uuid5(NAMESPACE_URL, f"kairo:genesis-capital:{proposal_id}"),
            cell_id=child_id,
            broker_snapshot_id=None,
            broker_account_id=None,
            economic_domain="SYNTHETIC",
            synthetic_provenance_id=manifest_id,
            settled_cash=self.SEED,
            safety_reserve=Decimal("0.00"),
            ownership_treasury_reserved=Decimal("0.00"),
            replication_reserve=Decimal("0.00"),
            committed_obligations=Decimal("0.00"),
            authorized_trading_cash=self.SEED,
            computed_at=occurred_at,
        ))
        self.session.add(CellTreasuryConfig(
            config_id=uuid5(NAMESPACE_URL, f"kairo:genesis-target:{proposal_id}"),
            cell_id=child_id,
            target_type=proposal.target_type,
            target_instrument_id=proposal.target_instrument_id,
            target_symbol=proposal.target_symbol,
            config_version=1,
            is_active=True,
            authorized_by="KAIRO-GENESIS",
            created_at=occurred_at,
        ))
        self.session.flush()

        for reservation in reservations:
            self.session.add(ReplicationCashConsumption(
                consumption_id=uuid5(
                    NAMESPACE_URL,
                    f"kairo:genesis-consumption:{proposal_id}:{reservation.allocation_id}",
                ),
                proposal_id=proposal_id,
                child_cell_id=child_id,
                allocation_id=reservation.allocation_id,
                consumed_usd=reservation.reserved_usd,
                is_synthetic=True,
                occurred_at=occurred_at,
            ))
            self.session.flush()
            self.session.add(ReplicationReservationEvent(
                event_id=uuid5(
                    NAMESPACE_URL, f"{reservation.reservation_id}:CONSUMED:{proposal_id}"
                ),
                reservation_id=reservation.reservation_id,
                event_type="CONSUMED",
                reason_code="GENESIS_SEED_CONSUMED",
                occurred_at=occurred_at,
            ))
            self.session.flush()

        self.session.add(CellGenesisEvent(
            genesis_id=uuid5(NAMESPACE_URL, f"kairo:cell-genesis:{proposal_id}"),
            proposal_id=proposal_id,
            parent_cell_id=proposal.parent_cell_id,
            child_cell_id=child_id,
            seed_capital_usd=self.SEED,
            is_synthetic=True,
            occurred_at=occurred_at,
        ))
        self.session.flush()
        self.session.add(ReplicationProposalEvent(
            event_id=uuid5(NAMESPACE_URL, f"{proposal_id}:EXECUTED"),
            proposal_id=proposal_id,
            state_from="AUTHORIZED",
            state_to="EXECUTED",
            reason_code="GENESIS_ATOMICALLY_COMPLETED",
            occurred_at=occurred_at,
        ))
        self.session.flush()
        return child
