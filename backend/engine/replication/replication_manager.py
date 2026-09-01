from datetime import datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from app.db.models.configuration import CellTreasuryConfig, RiskPolicy
from app.db.models.ledger import SiphonAllocation, SiphonEvent
from app.db.models.projections import CapitalCell
from app.db.models.replication import (
    CellReplicationProposal,
    ReplicationProposalEvent,
    ReplicationProposalReservation,
    ReplicationReservationEvent,
)
from engine.replication.models import (
    AllocationReservationRef,
    ProposalLifecycleState,
    ReplicationPolicyConfig,
    ReplicationProposalManifest,
    ReservationEventType,
)


class ReplicationManager:
    """Synthetic-only Step 3A qualification and reservation authority."""

    def __init__(
        self, session: Session, policy: ReplicationPolicyConfig | None = None
    ) -> None:
        self.session = session
        self.policy = policy or ReplicationPolicyConfig()

    def available_replication_cash(
        self, *, cell_id: UUID, is_synthetic: bool
    ) -> Decimal:
        allocations = list(self.session.scalars(self._allocation_query(
            cell_id=cell_id, is_synthetic=is_synthetic, lock=False
        )))
        return sum(
            (self._available_for_allocation(item) for item in allocations),
            Decimal("0.00"),
        )

    def create_proposal(
        self,
        *,
        parent_cell_id: UUID,
        proposed_child_code: str,
        is_synthetic: bool,
        occurred_at: datetime,
    ) -> CellReplicationProposal | None:
        if not is_synthetic:
            return None
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")

        parent = self.session.get(CapitalCell, parent_cell_id)
        if parent is None:
            raise ValueError("parent capital cell does not exist")
        risk_policy = self.session.get(RiskPolicy, parent.risk_policy_id)
        if risk_policy is None:
            raise ValueError("parent cell risk policy does not resolve")
        target = self.session.scalar(
            select(CellTreasuryConfig).where(
                CellTreasuryConfig.cell_id == parent_cell_id,
                CellTreasuryConfig.is_active.is_(True),
            )
        )
        if target is None:
            raise ValueError("parent cell has no active treasury target configuration")

        allocations = list(self.session.scalars(self._allocation_query(
            cell_id=parent_cell_id, is_synthetic=True, lock=True
        )))
        available = [
            (allocation, self._available_for_allocation(allocation))
            for allocation in allocations
        ]
        if sum((amount for _, amount in available), Decimal("0.00")) < self.policy.threshold_usd:
            return None

        remaining = self.policy.threshold_usd
        refs: list[AllocationReservationRef] = []
        for allocation, amount in available:
            if amount <= 0 or remaining <= 0:
                continue
            reserved = min(amount, remaining)
            refs.append(AllocationReservationRef(
                allocation_id=allocation.allocation_id, reserved_usd=reserved
            ))
            remaining -= reserved

        manifest = ReplicationProposalManifest(
            manifest_algorithm=self.policy.manifest_algorithm,
            parent_cell_id=parent.cell_id,
            proposed_child_code=proposed_child_code,
            capital_class=self.policy.capital_class,
            proposed_seed_capital_usd=self.policy.threshold_usd,
            strategy_identifier=parent.strategy_id,
            strategy_version=parent.strategy_version,
            risk_policy_identifier=risk_policy.policy_identifier,
            target_config_id=target.config_id,
            proposed_autonomy_tier=self.policy.proposed_autonomy_tier,
            is_synthetic=True,
            created_at=occurred_at,
            source_allocations=tuple(refs),
        )
        manifest_hash = manifest.sha256()
        proposal_id = uuid5(NAMESPACE_URL, f"kairo:replication-proposal:{manifest_hash}")
        existing = self.session.get(CellReplicationProposal, proposal_id)
        if existing is not None:
            return existing

        proposal = CellReplicationProposal(
            proposal_id=proposal_id,
            parent_cell_id=parent.cell_id,
            proposed_child_code=proposed_child_code,
            capital_class=self.policy.capital_class,
            proposed_seed_capital_usd=self.policy.threshold_usd,
            strategy_identifier=parent.strategy_id,
            strategy_version=parent.strategy_version,
            risk_policy_identifier=risk_policy.policy_identifier,
            target_config_id=target.config_id,
            proposed_autonomy_tier=self.policy.proposed_autonomy_tier,
            is_synthetic=True,
            manifest_hash=manifest_hash,
            created_at=occurred_at,
        )
        self.session.add(proposal)
        self.session.flush()
        self.session.add(ReplicationProposalEvent(
            event_id=uuid5(NAMESPACE_URL, f"{proposal_id}:PENDING_AUTHORIZATION"),
            proposal_id=proposal_id,
            state_from=ProposalLifecycleState.INITIAL.value,
            state_to=ProposalLifecycleState.PENDING_AUTHORIZATION.value,
            reason_code="REPLICATION_THRESHOLD_REACHED",
            occurred_at=occurred_at,
        ))
        self.session.flush()

        for ref in refs:
            reservation_id = uuid5(
                NAMESPACE_URL, f"{proposal_id}:reservation:{ref.allocation_id}"
            )
            self.session.add(ReplicationProposalReservation(
                reservation_id=reservation_id,
                proposal_id=proposal_id,
                allocation_id=ref.allocation_id,
                reserved_usd=ref.reserved_usd,
                occurred_at=occurred_at,
            ))
            self.session.flush()
            self.session.add(ReplicationReservationEvent(
                event_id=uuid5(NAMESPACE_URL, f"{reservation_id}:RESERVED"),
                reservation_id=reservation_id,
                event_type=ReservationEventType.RESERVED.value,
                reason_code="PROPOSAL_CAPITAL_RESERVED",
                occurred_at=occurred_at,
            ))
            self.session.flush()
        return proposal

    def release_proposal(self, *, proposal_id: UUID, occurred_at: datetime) -> None:
        reservations = list(self.session.scalars(
            select(ReplicationProposalReservation)
            .where(ReplicationProposalReservation.proposal_id == proposal_id)
            .with_for_update()
        ))
        for reservation in reservations:
            event_id = uuid5(
                NAMESPACE_URL,
                f"{reservation.reservation_id}:RELEASED:{occurred_at.isoformat()}",
            )
            self.session.add(ReplicationReservationEvent(
                event_id=event_id,
                reservation_id=reservation.reservation_id,
                event_type=ReservationEventType.RELEASED.value,
                reason_code="PROPOSAL_RELEASED",
                occurred_at=occurred_at,
            ))
        self.session.add(ReplicationProposalEvent(
            event_id=uuid5(NAMESPACE_URL, f"{proposal_id}:CANCELLED:{occurred_at.isoformat()}"),
            proposal_id=proposal_id,
            state_from=ProposalLifecycleState.PENDING_AUTHORIZATION.value,
            state_to=ProposalLifecycleState.CANCELLED.value,
            reason_code="PROPOSAL_CANCELLED",
            occurred_at=occurred_at,
        ))
        self.session.flush()

    def _allocation_query(self, *, cell_id: UUID, is_synthetic: bool, lock: bool):
        statement = (
            select(SiphonAllocation)
            .join(SiphonEvent, SiphonEvent.siphon_id == SiphonAllocation.siphon_id)
            .where(
                SiphonEvent.cell_id == cell_id,
                SiphonEvent.is_synthetic.is_(is_synthetic),
                SiphonAllocation.bucket_type == "REPLICATION_POOL",
            )
            .order_by(SiphonAllocation.occurred_at, SiphonAllocation.allocation_id)
        )
        return statement.with_for_update(of=SiphonAllocation) if lock else statement

    def _available_for_allocation(self, allocation: SiphonAllocation) -> Decimal:
        latest_type = (
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
        active = self.session.scalar(
            select(func.coalesce(func.sum(ReplicationProposalReservation.reserved_usd), 0))
            .where(
                ReplicationProposalReservation.allocation_id == allocation.allocation_id,
                latest_type == ReservationEventType.RESERVED.value,
            )
        )
        consumed = Decimal("0.00")
        if inspect(self.session.connection()).has_table("replication_cash_consumptions"):
            consumed = Decimal(self.session.scalar(text(
                "SELECT COALESCE(SUM(consumed_usd), 0.00) "
                "FROM replication_cash_consumptions WHERE allocation_id = :allocation_id"
            ), {"allocation_id": allocation.allocation_id}))
        return Decimal(allocation.allocated_usd) - Decimal(consumed) - Decimal(active)
