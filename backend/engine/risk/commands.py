from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import OrderPurpose, OrderSide
from engine.risk.models import OperationalState


class ControlCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    command_id: UUID
    correlation_key: str
    state_event_id: UUID
    status: Literal["REQUESTED"] = "REQUESTED"


class CancelOrderCommand(ControlCommand):
    command_type: Literal["CANCEL_REQUESTED"] = "CANCEL_REQUESTED"
    kairo_order_id: UUID
    broker_account_id: UUID


class EmergencyExitCommand(ControlCommand):
    command_type: Literal["EMERGENCY_EXIT"] = "EMERGENCY_EXIT"
    position_id: UUID
    cell_id: UUID
    broker_account_id: UUID
    instrument_id: UUID
    side: OrderSide
    quantity: Decimal = Field(gt=0)
    order_purpose: Literal[OrderPurpose.EMERGENCY_EXIT] = OrderPurpose.EMERGENCY_EXIT


class StateTransitionCommand(ControlCommand):
    command_type: Literal["STATE_TRANSITION"] = "STATE_TRANSITION"
    previous_state: OperationalState
    new_state: OperationalState
    trigger_reason: str
