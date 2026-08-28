from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.projections import CapitalCell, CurrentPosition


class CapitalCellProjectionRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, cell_id: UUID) -> CapitalCell | None:
        return self.session.get(CapitalCell, cell_id)

    def save(self, cell: CapitalCell) -> CapitalCell:
        merged = self.session.merge(cell)
        self.session.flush()
        return merged


class CurrentPositionProjectionRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(
        self, *, cell_id: UUID, broker_account_id: UUID, instrument_id: UUID
    ) -> CurrentPosition | None:
        return self.session.scalar(
            select(CurrentPosition).where(
                CurrentPosition.cell_id == cell_id,
                CurrentPosition.broker_account_id == broker_account_id,
                CurrentPosition.instrument_id == instrument_id,
            )
        )

    def set_position(
        self,
        *,
        cell_id: UUID,
        broker_account_id: UUID,
        instrument_id: UUID,
        quantity: Decimal,
        average_price: Decimal,
    ) -> CurrentPosition:
        position = self.get(
            cell_id=cell_id,
            broker_account_id=broker_account_id,
            instrument_id=instrument_id,
        )
        if position is None:
            position = CurrentPosition(
                cell_id=cell_id,
                broker_account_id=broker_account_id,
                instrument_id=instrument_id,
                quantity=quantity,
                average_price=average_price,
            )
            self.session.add(position)
        else:
            position.quantity = quantity
            position.average_price = average_price
        self.session.flush()
        return position
