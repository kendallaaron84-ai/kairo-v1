from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.broker import BrokerInstrumentCapability
from app.db.models.configuration import Instrument, StrategyRegistry
from app.db.models.ledger import KairoCapitalAuthorizationRecord, OrderIntent, RiskDecision
from app.db.models.projections import CapitalCell, CurrentPosition
from app.db.models.risk import RiskGovernorState, RiskInstrumentMark, RiskStateEvent
from app.domain.enums import OrderPurpose, OrderSide
from engine.risk.commands import (
    CancelOrderCommand,
    ControlCommand,
    EmergencyExitCommand,
    StateTransitionCommand,
)
from engine.risk.exceptions import InvalidStateTransition, RiskGovernorError
from engine.risk.intent_classifier import classify_intent
from engine.risk.models import (
    BrokerCapabilityProfile,
    DecisionVerdict,
    DisqualificationReason,
    ExecutionEnvironment,
    FillAccountingEvent,
    IntentEvaluationInput,
    IntentRiskMetrics,
    InstrumentRiskProfile,
    MarketMark,
    OperationalState,
    PendingRiskOrder,
    PositionSnapshot,
    RiskClassification,
    RiskEvaluationRequest,
    RiskEvaluationResult,
    RiskSessionSpec,
    StrategyClearance,
    TransitionReason,
)
from engine.risk.pnl_tracker import apply_fill, mark_to_market
from engine.risk.state_machine import RiskStateMachine
from engine.execution.virtual_clock import ReplayIdentityFactory, VirtualClock


LOSS_LIMIT = Decimal("-6.00")
PROFIT_CEILING = Decimal("20.00")
MAX_QUOTE_AGE = timedelta(seconds=1.5)
EXIT_PURPOSES = {
    OrderPurpose.TAKE_PROFIT,
    OrderPurpose.STOP_LOSS,
    OrderPurpose.EMERGENCY_EXIT,
}


class RiskGovernor:
    """Persistent deterministic capital authority; never a broker executor."""

    def __init__(
        self,
        session: Session,
        *,
        clock: VirtualClock | None = None,
        identities: ReplayIdentityFactory | None = None,
    ):
        self.session = session
        self.clock = clock
        self.identities = identities
        self.state_machine = RiskStateMachine(
            session, clock=clock, identities=identities
        )

    def _now(self) -> datetime:
        return self.clock.now() if self.clock is not None else datetime.now(UTC)

    def _decision_id(self, request: RiskEvaluationRequest) -> UUID:
        if self.identities is None:
            return uuid4()
        return self.identities.generate_id(
            "risk_decision",
            request.intent.instrument_id,
            self._now(),
            parent_id=request.intent.intent_id,
        )

    def initialize_session(self, spec: RiskSessionSpec) -> RiskGovernorState:
        return self.state_machine.initialize_session(spec)

    def current_state(self) -> RiskGovernorState | None:
        return self.state_machine.current_state()

    def arm(self, *, authorized_cash_usd: Decimal) -> StateTransitionCommand:
        state = self.state_machine.lock_state()
        event = self.state_machine.transition(
            state,
            OperationalState.ARMED,
            TransitionReason.MANUAL_ARM,
            authorized_cash_usd,
        )
        if event is None:
            raise InvalidStateTransition("Governor is already armed")
        return self._transition_command(event)

    def halt_trading(self, *, authorized_cash_usd: Decimal) -> StateTransitionCommand:
        state = self.state_machine.lock_state()
        event = self.state_machine.transition(
            state,
            OperationalState.MANUAL_PAUSE,
            TransitionReason.MANUAL_PAUSE,
            authorized_cash_usd,
        )
        if event is None:
            raise InvalidStateTransition("Governor is already manually paused")
        return self._transition_command(event)

    def flatten_all(
        self,
        *,
        authorized_cash_usd: Decimal,
        open_positions: list[PositionSnapshot],
        pending_orders: list[PendingRiskOrder],
    ) -> tuple[ControlCommand, ...]:
        state = self.state_machine.lock_state()
        event = self.state_machine.transition(
            state,
            OperationalState.HALTED_HARD,
            TransitionReason.MANUAL_FLATTEN_ALL,
            authorized_cash_usd,
        )
        if event is None:
            return ()
        return self._liquidation_commands(event, open_positions, pending_orders)

    def reconcile_confirmed_positions(
        self, *, open_positions: list[PositionSnapshot], authorized_cash_usd: Decimal
    ) -> StateTransitionCommand | None:
        state = self.state_machine.lock_state()
        if OperationalState(state.operational_state) is not OperationalState.LOCKED_FOR_DAY:
            return None
        if any(position.quantity != 0 for position in open_positions):
            return None
        event = self.state_machine.transition(
            state,
            OperationalState.FLAT_LOCKED,
            TransitionReason.CONFIRMED_FLAT,
            authorized_cash_usd,
        )
        return self._transition_command(event) if event else None

    def evaluate(self, request: RiskEvaluationRequest) -> RiskEvaluationResult:
        state = self.state_machine.lock_state()
        request, canonical_reason = self._canonicalize_request(request)
        classification, metrics = classify_intent(request)
        reason = canonical_reason or self._evaluate_reason(
            state, request, classification, metrics
        )
        verdict = (
            DecisionVerdict.AUTHORIZED
            if reason is DisqualificationReason.NONE
            else DecisionVerdict.REJECTED
        )
        decision_id = self._decision_id(request)
        self.session.add(
            RiskDecision(
                decision_id=decision_id,
                intent_id=request.intent.intent_id,
                session_id=state.current_session_id,
                verdict=verdict.value,
                reason_code=reason.value,
                operational_state=state.operational_state,
                intent_classification=classification.value,
                session_net_pnl=state.session_net_pnl,
                authorized_cash_usd=request.authorized_trading_cash,
                requested_cash_usd=metrics.requested_cash_usd,
                projected_exposure_usd=metrics.projected_exposure_usd,
                max_contractual_loss_usd=metrics.max_contractual_loss_usd,
                details={
                    "source_timestamp": request.market_mark.source_timestamp.isoformat(),
                    "received_at": request.market_mark.received_at.isoformat(),
                    "projected_quantity": str(metrics.projected_quantity),
                    "order_purpose": request.intent.order_purpose.value,
                },
                decided_at=self._now(),
            )
        )
        self.session.flush()
        return RiskEvaluationResult(
            decision_id=decision_id,
            verdict=verdict,
            reason=reason,
            classification=classification,
            metrics=metrics,
            operational_state=OperationalState(state.operational_state),
            session_id=state.current_session_id,
        )

    def _canonicalize_request(
        self, request: RiskEvaluationRequest
    ) -> tuple[RiskEvaluationRequest, DisqualificationReason | None]:
        supplied_intent = request.intent
        stored_intent = self.session.get(OrderIntent, supplied_intent.intent_id)
        if stored_intent is None:
            return request, DisqualificationReason.POSITION_IDENTITY_MISMATCH
        intent = IntentEvaluationInput(
            intent_id=stored_intent.intent_id,
            cell_id=stored_intent.cell_id,
            strategy_id=stored_intent.strategy_id,
            strategy_version=stored_intent.strategy_version,
            instrument_id=stored_intent.instrument_id,
            order_purpose=stored_intent.order_purpose,
            side=stored_intent.side,
            target_notional_usd=stored_intent.target_notional_usd,
            target_quantity=stored_intent.target_quantity,
            order_type=stored_intent.order_type,
        )
        instrument = self.session.get(Instrument, intent.instrument_id)
        strategy = self.session.get(
            StrategyRegistry, (intent.strategy_id, intent.strategy_version)
        )
        cell = self.session.get(CapitalCell, intent.cell_id)
        if instrument is None or cell is None:
            return request, DisqualificationReason.POSITION_IDENTITY_MISMATCH
        if request.market_mark.instrument_id != intent.instrument_id:
            return request, DisqualificationReason.POSITION_IDENTITY_MISMATCH

        capability = self.session.scalar(
            select(BrokerInstrumentCapability)
            .where(
                BrokerInstrumentCapability.broker_account_id
                == request.broker_account_id,
                BrokerInstrumentCapability.instrument_id == intent.instrument_id,
                BrokerInstrumentCapability.retired_at.is_(None),
            )
            .order_by(BrokerInstrumentCapability.effective_from.desc())
            .limit(1)
        )
        position = self.session.scalar(
            select(CurrentPosition).where(
                CurrentPosition.cell_id == intent.cell_id,
                CurrentPosition.broker_account_id == request.broker_account_id,
                CurrentPosition.instrument_id == intent.instrument_id,
            )
        )
        authorization = self.session.scalar(
            select(KairoCapitalAuthorizationRecord)
            .where(
                KairoCapitalAuthorizationRecord.cell_id == intent.cell_id,
                KairoCapitalAuthorizationRecord.broker_account_id
                == request.broker_account_id,
            )
            .order_by(KairoCapitalAuthorizationRecord.computed_at.desc())
            .limit(1)
        )
        clearance_value = (
            strategy.configuration.get("clearance") if strategy is not None else None
        )
        try:
            clearance = StrategyClearance(clearance_value)
        except (TypeError, ValueError):
            clearance = request.strategy_clearance
            clearance_reason = DisqualificationReason.STRATEGY_CLEARANCE_MISMATCH
        else:
            clearance_reason = None

        canonical_capability = (
            BrokerCapabilityProfile(
                broker_account_id=capability.broker_account_id,
                instrument_id=capability.instrument_id,
                can_trade=capability.can_trade,
                can_fractional=capability.can_fractional,
                can_short=capability.can_short,
                notional_orders_supported=capability.notional_orders_supported,
                options_supported=capability.options_supported,
                extended_hours_supported=capability.extended_hours_supported,
                minimum_quantity=capability.minimum_quantity,
            )
            if capability is not None
            else None
        )
        canonical_position = (
            PositionSnapshot(
                position_id=position.position_id,
                cell_id=position.cell_id,
                broker_account_id=position.broker_account_id,
                instrument_id=position.instrument_id,
                quantity=position.quantity,
                average_price=position.average_price,
                contract_multiplier=instrument.contract_multiplier or Decimal("1"),
            )
            if position is not None
            else None
        )
        mark = request.market_mark.mark_price
        current_exposure = (
            abs(position.quantity)
            * mark
            * (instrument.contract_multiplier or Decimal("1"))
            if position is not None
            else Decimal("0")
        )
        canonical = request.model_copy(
            update={
                "intent": intent,
                "instrument": InstrumentRiskProfile(
                    instrument_id=instrument.instrument_id,
                    asset_class=instrument.asset_class,
                    contract_multiplier=instrument.contract_multiplier,
                ),
                "capability": canonical_capability,
                "current_position": canonical_position,
                "strategy_clearance": clearance,
                "authorized_trading_cash": (
                    authorization.authorized_trading_cash
                    if authorization is not None
                    else Decimal("0")
                ),
                "authorized_exposure_usd": cell.seed_capital,
                "current_exposure_usd": current_exposure,
            }
        )
        return canonical, clearance_reason

    def record_fill_accounting(
        self,
        event: FillAccountingEvent,
        *,
        authorized_cash_usd: Decimal,
        open_positions: list[PositionSnapshot] | None = None,
        pending_orders: list[PendingRiskOrder] | None = None,
    ) -> tuple[ControlCommand, ...]:
        state = self.state_machine.lock_state()
        updated = apply_fill(self.state_machine.pnl_snapshot(state), event)
        self.state_machine.persist_pnl(state, updated)
        return self._apply_pnl_boundary(
            state,
            authorized_cash_usd,
            open_positions or [],
            pending_orders or [],
        )

    def record_market_mark(
        self,
        mark: MarketMark,
        *,
        positions: list[PositionSnapshot],
        authorized_cash_usd: Decimal,
        pending_orders: list[PendingRiskOrder] | None = None,
    ) -> tuple[ControlCommand, ...]:
        if mark.quote_age() is None:
            raise RiskGovernorError("market mark has an invalid/future timestamp")
        state = self.state_machine.lock_state()
        persisted = self.session.get(
            RiskInstrumentMark, (state.current_session_id, mark.instrument_id)
        )
        if persisted is None:
            persisted = RiskInstrumentMark(
                session_id=state.current_session_id,
                instrument_id=mark.instrument_id,
                mark_price=mark.mark_price,
                source_timestamp=mark.source_timestamp,
                received_at=mark.received_at,
                updated_at=self._now(),
            )
            self.session.add(persisted)
        elif mark.source_timestamp >= persisted.source_timestamp:
            persisted.mark_price = mark.mark_price
            persisted.source_timestamp = mark.source_timestamp
            persisted.received_at = mark.received_at
            persisted.updated_at = self._now()
        self.session.flush()

        canonical_positions = self._canonical_open_positions()
        persisted_marks = self.session.scalars(
            select(RiskInstrumentMark).where(
                RiskInstrumentMark.session_id == state.current_session_id
            ).with_for_update()
        )
        latest_marks = {
            persisted_mark.instrument_id: persisted_mark.mark_price
            for persisted_mark in persisted_marks
        }
        required_instruments = {
            position.instrument_id for position in canonical_positions
        }
        if not required_instruments.issubset(latest_marks):
            # Persist the new fact, but retain the last complete portfolio P&L until
            # every open instrument has an observed mark. Never fabricate a quote or
            # replace the aggregate with an incomplete subset.
            return ()
        updated = mark_to_market(
            self.state_machine.pnl_snapshot(state), latest_marks, canonical_positions
        )
        self.state_machine.persist_pnl(state, updated)
        return self._apply_pnl_boundary(
            state,
            authorized_cash_usd,
            canonical_positions,
            pending_orders or [],
        )

    def _canonical_open_positions(self) -> list[PositionSnapshot]:
        rows = self.session.execute(
            select(CurrentPosition, Instrument)
            .join(Instrument, Instrument.instrument_id == CurrentPosition.instrument_id)
            .where(CurrentPosition.quantity != 0)
            .with_for_update(of=CurrentPosition)
        ).all()
        return [
            PositionSnapshot(
                position_id=position.position_id,
                cell_id=position.cell_id,
                broker_account_id=position.broker_account_id,
                instrument_id=position.instrument_id,
                quantity=position.quantity,
                average_price=position.average_price,
                contract_multiplier=instrument.contract_multiplier or Decimal("1"),
            )
            for position, instrument in rows
        ]

    def _evaluate_reason(
        self,
        state: RiskGovernorState,
        request: RiskEvaluationRequest,
        classification: RiskClassification,
        metrics: IntentRiskMetrics,
    ) -> DisqualificationReason:
        intent = request.intent
        position = request.current_position
        is_option = request.instrument.asset_class == "OPTION"
        if is_option and intent.target_notional_usd is not None:
            return DisqualificationReason.OPTION_NOTIONAL_SIZING_PROHIBITED

        if intent.order_purpose in EXIT_PURPOSES:
            return self._exit_safety_reason(state, request, classification, metrics)
        if classification is RiskClassification.RISK_REDUCING:
            return self._exit_safety_reason(state, request, classification, metrics)

        operational = OperationalState(state.operational_state)
        if operational is OperationalState.LOCKED_FOR_DAY:
            return DisqualificationReason.PROFIT_CEILING_REACHED
        if operational in {OperationalState.HALTED_HARD, OperationalState.FLAT_LOCKED}:
            return DisqualificationReason.SYSTEM_HALTED
        if operational is not OperationalState.ARMED:
            return DisqualificationReason.NOT_ARMED

        quote_age = request.market_mark.quote_age()
        if quote_age is None:
            return DisqualificationReason.INVALID_MARKET_TIMESTAMP
        if quote_age > MAX_QUOTE_AGE:
            return DisqualificationReason.MARKET_DATA_STALE
        if state.session_net_pnl <= LOSS_LIMIT:
            return DisqualificationReason.SESSION_LOSS_LIMIT_REACHED
        if state.session_net_pnl >= PROFIT_CEILING:
            return DisqualificationReason.PROFIT_CEILING_REACHED
        if not self._clearance_matches(
            request.strategy_clearance, request.execution_environment
        ):
            return DisqualificationReason.STRATEGY_CLEARANCE_MISMATCH
        if not self._capability_allows(request, metrics):
            return DisqualificationReason.BROKER_CAPABILITY_UNSUPPORTED
        if metrics.projected_exposure_usd > request.authorized_exposure_usd:
            return DisqualificationReason.CELL_EXPOSURE_EXCEEDED
        if metrics.requested_cash_usd > request.authorized_trading_cash:
            return DisqualificationReason.INSUFFICIENT_AUTHORIZED_CASH
        return DisqualificationReason.NONE

    def _exit_safety_reason(
        self,
        state: RiskGovernorState,
        request: RiskEvaluationRequest,
        classification: RiskClassification,
        metrics: IntentRiskMetrics,
    ) -> DisqualificationReason:
        intent = request.intent
        position = request.current_position
        operational = OperationalState(state.operational_state)
        if (
            operational is OperationalState.HALTED_HARD
            and intent.order_purpose is not OrderPurpose.EMERGENCY_EXIT
        ):
            return DisqualificationReason.SYSTEM_HALTED
        if position is None or position.quantity == 0:
            return DisqualificationReason.NO_CLOSABLE_INVENTORY
        if (
            position.cell_id != intent.cell_id
            or position.broker_account_id != request.broker_account_id
            or position.instrument_id != intent.instrument_id
        ):
            return DisqualificationReason.POSITION_IDENTITY_MISMATCH
        reducing_side = (
            intent.side is OrderSide.SELL if position.quantity > 0 else intent.side is OrderSide.BUY
        )
        if not reducing_side or classification is RiskClassification.RISK_INCREASING:
            return DisqualificationReason.EXIT_WOULD_INCREASE_RISK
        if metrics.requested_quantity > abs(position.quantity):
            return DisqualificationReason.EXIT_EXCEEDS_POSITION_QTY
        if abs(metrics.projected_quantity) > abs(position.quantity):
            return DisqualificationReason.EXIT_WOULD_INCREASE_RISK
        return DisqualificationReason.NONE

    def _capability_allows(
        self, request: RiskEvaluationRequest, metrics: IntentRiskMetrics
    ) -> bool:
        capability = request.capability
        if capability is None:
            return False
        if (
            capability.broker_account_id != request.broker_account_id
            or capability.instrument_id != request.intent.instrument_id
            or request.instrument.instrument_id != request.intent.instrument_id
            or not capability.can_trade
        ):
            return False
        if request.instrument.asset_class == "OPTION" and not capability.options_supported:
            return False
        if (
            request.intent.target_notional_usd is not None
            and not capability.notional_orders_supported
        ):
            return False
        if metrics.requested_quantity % Decimal("1") != 0 and not capability.can_fractional:
            return False
        if metrics.projected_quantity < 0 and not capability.can_short:
            return False
        if (
            capability.minimum_quantity is not None
            and metrics.requested_quantity < capability.minimum_quantity
        ):
            return False
        if request.extended_hours_requested and not capability.extended_hours_supported:
            return False
        return True

    @staticmethod
    def _clearance_matches(
        clearance: StrategyClearance, environment: ExecutionEnvironment
    ) -> bool:
        allowed = {
            StrategyClearance.PAPER_ONLY: {ExecutionEnvironment.PAPER},
            StrategyClearance.SHADOW: {
                ExecutionEnvironment.PAPER,
                ExecutionEnvironment.SHADOW,
            },
            StrategyClearance.LIVE: set(ExecutionEnvironment),
        }
        return environment in allowed[clearance]

    def _apply_pnl_boundary(
        self,
        state: RiskGovernorState,
        authorized_cash_usd: Decimal,
        positions: list[PositionSnapshot],
        pending_orders: list[PendingRiskOrder],
    ) -> tuple[ControlCommand, ...]:
        operational = OperationalState(state.operational_state)
        if state.session_net_pnl <= LOSS_LIMIT and operational not in {
            OperationalState.HALTED_HARD,
            OperationalState.FLAT_LOCKED,
        }:
            event = self.state_machine.transition(
                state,
                OperationalState.HALTED_HARD,
                TransitionReason.SESSION_LOSS_LIMIT,
                authorized_cash_usd,
            )
            return self._liquidation_commands(event, positions, pending_orders) if event else ()
        if (
            state.session_net_pnl >= PROFIT_CEILING
            and operational is OperationalState.ARMED
        ):
            event = self.state_machine.transition(
                state,
                OperationalState.LOCKED_FOR_DAY,
                TransitionReason.SESSION_PROFIT_CEILING,
                authorized_cash_usd,
            )
            return (self._transition_command(event),) if event else ()
        return ()

    def _liquidation_commands(
        self,
        event: RiskStateEvent,
        positions: list[PositionSnapshot],
        pending_orders: list[PendingRiskOrder],
    ) -> tuple[ControlCommand, ...]:
        commands: list[ControlCommand] = [self._transition_command(event)]
        for order in pending_orders:
            if order.classification is RiskClassification.RISK_INCREASING:
                correlation = f"{event.session_id}:cancel:{order.kairo_order_id}"
                commands.append(
                    CancelOrderCommand(
                        command_id=uuid5(NAMESPACE_URL, correlation),
                        correlation_key=correlation,
                        state_event_id=event.event_id,
                        kairo_order_id=order.kairo_order_id,
                        broker_account_id=order.broker_account_id,
                    )
                )
        for position in positions:
            if position.quantity == 0:
                continue
            correlation = (
                f"{event.session_id}:emergency:{position.position_id}:{abs(position.quantity)}"
            )
            commands.append(
                EmergencyExitCommand(
                    command_id=uuid5(NAMESPACE_URL, correlation),
                    correlation_key=correlation,
                    state_event_id=event.event_id,
                    position_id=position.position_id,
                    cell_id=position.cell_id,
                    broker_account_id=position.broker_account_id,
                    instrument_id=position.instrument_id,
                    side=OrderSide.SELL if position.quantity > 0 else OrderSide.BUY,
                    quantity=abs(position.quantity),
                )
            )
        return tuple(commands)

    @staticmethod
    def _transition_command(event: RiskStateEvent) -> StateTransitionCommand:
        correlation = f"{event.session_id}:transition:{event.event_id}"
        return StateTransitionCommand(
            command_id=uuid5(NAMESPACE_URL, correlation),
            correlation_key=correlation,
            state_event_id=event.event_id,
            previous_state=OperationalState(event.previous_state),
            new_state=OperationalState(event.new_state),
            trigger_reason=event.trigger_reason,
        )
