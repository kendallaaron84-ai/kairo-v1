from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.replication import (
    CellReplicationProposal,
    ReplicationAuthorization,
    ReplicationProposalEvent,
    ReplicationProposalReservation,
    ReplicationReservationEvent,
)
from engine.replication.models import AuthorizationDecision


class HumanAuthorizationService:
    """Operator-only append authority for byte-exact proposal decisions."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def authorize(
        self,
        *,
        proposal_id: UUID,
        manifest_hash: str,
        decision: AuthorizationDecision,
        authorized_by: str,
        authorization_method: str,
        authorized_at: datetime,
    ) -> ReplicationAuthorization:
        if authorized_at.tzinfo is None or authorized_at.utcoffset() is None:
            raise ValueError("authorized_at must be timezone-aware")
        proposal = self.session.scalar(
            select(CellReplicationProposal)
            .where(CellReplicationProposal.proposal_id == proposal_id)
            .with_for_update()
        )
        if proposal is None:
            raise ValueError("replication proposal does not exist")
        if proposal.manifest_hash != manifest_hash:
            raise ValueError("authorization manifest hash does not match proposal")

        existing = self.session.scalar(
            select(ReplicationAuthorization).where(
                ReplicationAuthorization.proposal_id == proposal_id
            )
        )
        if existing is not None:
            if (
                existing.manifest_hash == manifest_hash
                and existing.decision == decision.value
                and existing.authorized_by == authorized_by
                and existing.authorization_method == authorization_method
            ):
                return existing
            raise ValueError("proposal already has a conflicting immutable authorization")

        current_state = self.session.scalar(
            select(ReplicationProposalEvent.state_to)
            .where(ReplicationProposalEvent.proposal_id == proposal_id)
            .order_by(
                ReplicationProposalEvent.occurred_at.desc(),
                ReplicationProposalEvent.event_id.desc(),
            )
            .limit(1)
        )
        if current_state != "PENDING_AUTHORIZATION":
            raise ValueError(f"proposal is not pending authorization: {current_state}")

        authorization = ReplicationAuthorization(
            authorization_id=uuid5(NAMESPACE_URL, f"kairo:replication-auth:{proposal_id}"),
            proposal_id=proposal_id,
            manifest_hash=manifest_hash,
            decision=decision.value,
            authorized_by=authorized_by,
            authorization_method=authorization_method,
            authorized_at=authorized_at,
        )
        self.session.add(authorization)
        self.session.flush()

        if decision is AuthorizationDecision.REJECT:
            reservations = list(self.session.scalars(
                select(ReplicationProposalReservation)
                .where(ReplicationProposalReservation.proposal_id == proposal_id)
                .with_for_update()
            ))
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
                if latest == "RESERVED":
                    self.session.add(ReplicationReservationEvent(
                        event_id=uuid5(
                            NAMESPACE_URL,
                            f"{reservation.reservation_id}:RELEASED:{authorization.authorization_id}",
                        ),
                        reservation_id=reservation.reservation_id,
                        event_type="RELEASED",
                        reason_code="HUMAN_REPLICATION_REJECTED",
                        occurred_at=authorized_at,
                    ))

        target_state = (
            "AUTHORIZED" if decision is AuthorizationDecision.APPROVE else "REJECTED"
        )
        self.session.add(ReplicationProposalEvent(
            event_id=uuid5(NAMESPACE_URL, f"{proposal_id}:{target_state}"),
            proposal_id=proposal_id,
            state_from="PENDING_AUTHORIZATION",
            state_to=target_state,
            reason_code=f"HUMAN_{decision.value}",
            occurred_at=authorized_at,
        ))
        self.session.flush()
        return authorization
