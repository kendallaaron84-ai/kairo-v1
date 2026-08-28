from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from app.db.models.ledger import CellEvent, Fill, OrderIntent, OrderObservation
from app.repositories.ledger.base import AppendOnlyRepository


class CellEventRepository(AppendOnlyRepository[CellEvent]):
    model = CellEvent
    id_attribute = "event_id"

    def list_by_cell(self, cell_id: UUID) -> list[CellEvent]:
        return self._all(
            select(CellEvent).where(CellEvent.cell_id == cell_id).order_by(CellEvent.occurred_at)
        )

    def list_between(self, start: datetime, end: datetime) -> list[CellEvent]:
        return self.list_by_time_range(column_name="occurred_at", start=start, end=end)


class OrderIntentRepository(AppendOnlyRepository[OrderIntent]):
    model = OrderIntent
    id_attribute = "intent_id"

    def list_by_cell(self, cell_id: UUID) -> list[OrderIntent]:
        return self._all(
            select(OrderIntent).where(OrderIntent.cell_id == cell_id).order_by(OrderIntent.created_at)
        )


class OrderObservationRepository(AppendOnlyRepository[OrderObservation]):
    model = OrderObservation
    id_attribute = "observation_id"

    def list_by_order(self, kairo_order_id: UUID) -> list[OrderObservation]:
        return self._all(
            select(OrderObservation)
            .where(OrderObservation.kairo_order_id == kairo_order_id)
            .order_by(OrderObservation.observed_at)
        )


class FillRepository(AppendOnlyRepository[Fill]):
    model = Fill
    id_attribute = "fill_id"

    def list_by_order(self, kairo_order_id: UUID) -> list[Fill]:
        return self._all(
            select(Fill).where(Fill.kairo_order_id == kairo_order_id).order_by(Fill.filled_at)
        )
