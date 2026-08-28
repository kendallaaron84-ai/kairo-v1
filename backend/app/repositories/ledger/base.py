from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.db.base import Base

LedgerModel = TypeVar("LedgerModel", bound=Base)


class AppendOnlyRepository(Generic[LedgerModel]):
    """Facts may only be appended and queried; mutation APIs intentionally do not exist."""

    model: type[LedgerModel]
    id_attribute: str

    def __init__(self, session: Session):
        self.session = session

    def append(self, record: LedgerModel) -> LedgerModel:
        self.session.add(record)
        self.session.flush()
        return record

    def get_by_id(self, record_id: UUID) -> LedgerModel | None:
        column = getattr(self.model, self.id_attribute)
        return self.session.scalar(select(self.model).where(column == record_id))

    def _all(self, statement: Select[tuple[LedgerModel]]) -> list[LedgerModel]:
        return list(self.session.scalars(statement))

    def list_by_time_range(
        self, *, column_name: str, start: datetime, end: datetime
    ) -> list[LedgerModel]:
        column = getattr(self.model, column_name)
        return self._all(select(self.model).where(column >= start, column < end).order_by(column))
