from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.risk import RiskGovernorState, RiskSession, RiskStateEvent
from engine.risk.exceptions import InvalidStateTransition, RiskSessionNotInitialized
from engine.risk.models import OperationalState, PnLSnapshot, RiskSessionSpec, TransitionReason


ALLOWED_TRANSITIONS: dict[OperationalState, set[OperationalState]] = {
    OperationalState.DISARMED: {
        OperationalState.ARMED,
        OperationalState.MANUAL_PAUSE,
        OperationalState.HALTED_HARD,
    },
    OperationalState.ARMED: {
        OperationalState.MANUAL_PAUSE,
        OperationalState.LOCKED_FOR_DAY,
        OperationalState.HALTED_HARD,
    },
    OperationalState.MANUAL_PAUSE: {
        OperationalState.ARMED,
        OperationalState.HALTED_HARD,
    },
    OperationalState.LOCKED_FOR_DAY: {
        OperationalState.FLAT_LOCKED,
        OperationalState.HALTED_HARD,
    },
    OperationalState.HALTED_HARD: set(),
    OperationalState.FLAT_LOCKED: set(),
}


class RiskStateMachine:
    def __init__(self, session: Session):
        self.session = session

    def lock_state(self) -> RiskGovernorState:
        state = self.session.scalar(
            select(RiskGovernorState)
            .where(RiskGovernorState.singleton_key == 1)
            .with_for_update()
        )
        if state is None:
            raise RiskSessionNotInitialized("initialize an explicit risk session first")
        return state

    def current_state(self) -> RiskGovernorState | None:
        return self.session.get(RiskGovernorState, 1)

    def initialize_session(self, spec: RiskSessionSpec) -> RiskGovernorState:
        risk_session = self.session.get(RiskSession, spec.session_id)
        if risk_session is None:
            risk_session = RiskSession(
                session_id=spec.session_id,
                trading_date=spec.trading_date,
                market_timezone=spec.market_timezone,
                session_open=spec.session_open,
                session_close=spec.session_close,
            )
            self.session.add(risk_session)
            self.session.flush()

        state = self.session.scalar(
            select(RiskGovernorState)
            .where(RiskGovernorState.singleton_key == 1)
            .with_for_update()
        )
        if state is not None and state.current_session_id == spec.session_id:
            return state

        previous = (
            OperationalState(state.operational_state)
            if state is not None
            else OperationalState.DISARMED
        )
        now = datetime.now(UTC)
        if state is None:
            state = RiskGovernorState(
                singleton_key=1,
                current_session_id=spec.session_id,
                operational_state=OperationalState.DISARMED.value,
                session_realized_pnl=Decimal("0"),
                session_unrealized_pnl=Decimal("0"),
                session_fees_usd=Decimal("0"),
                session_slippage_usd=Decimal("0"),
                session_net_pnl=Decimal("0"),
                last_state_change_at=now,
                updated_at=now,
            )
            self.session.add(state)
        else:
            state.current_session_id = spec.session_id
            state.operational_state = OperationalState.DISARMED.value
            state.session_realized_pnl = Decimal("0")
            state.session_unrealized_pnl = Decimal("0")
            state.session_fees_usd = Decimal("0")
            state.session_slippage_usd = Decimal("0")
            state.session_net_pnl = Decimal("0")
            state.last_state_change_at = now
            state.updated_at = now
        self.session.add(
            RiskStateEvent(
                event_id=uuid4(),
                session_id=spec.session_id,
                previous_state=previous.value,
                new_state=OperationalState.DISARMED.value,
                trigger_reason=TransitionReason.SESSION_INITIALIZED.value,
                current_session_net_pnl=Decimal("0"),
                authorized_cash_usd=Decimal("0"),
            )
        )
        self.session.flush()
        return state

    def transition(
        self,
        state: RiskGovernorState,
        new_state: OperationalState,
        reason: TransitionReason,
        authorized_cash_usd: Decimal,
    ) -> RiskStateEvent | None:
        previous = OperationalState(state.operational_state)
        if previous is new_state:
            return None
        if new_state not in ALLOWED_TRANSITIONS[previous]:
            raise InvalidStateTransition(f"{previous.value} cannot transition to {new_state.value}")
        event = RiskStateEvent(
            event_id=uuid4(),
            session_id=state.current_session_id,
            previous_state=previous.value,
            new_state=new_state.value,
            trigger_reason=reason.value,
            current_session_net_pnl=state.session_net_pnl,
            authorized_cash_usd=authorized_cash_usd,
        )
        now = datetime.now(UTC)
        self.session.add(event)
        state.operational_state = new_state.value
        state.last_state_change_at = now
        state.updated_at = now
        self.session.flush()
        return event

    def pnl_snapshot(self, state: RiskGovernorState) -> PnLSnapshot:
        return PnLSnapshot(
            realized_pnl=state.session_realized_pnl,
            unrealized_pnl=state.session_unrealized_pnl,
            fees_usd=state.session_fees_usd,
            slippage_usd=state.session_slippage_usd,
            net_pnl=state.session_net_pnl,
        )

    def persist_pnl(self, state: RiskGovernorState, snapshot: PnLSnapshot) -> None:
        state.session_realized_pnl = snapshot.realized_pnl
        state.session_unrealized_pnl = snapshot.unrealized_pnl
        state.session_fees_usd = snapshot.fees_usd
        state.session_slippage_usd = snapshot.slippage_usd
        state.session_net_pnl = snapshot.net_pnl
        state.updated_at = datetime.now(UTC)
        self.session.flush()
