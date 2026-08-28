from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.configuration import Instrument, StrategyRegistry


class InstrumentRepository:
    def __init__(self, session: Session):
        self.session = session

    def add(self, instrument: Instrument) -> Instrument:
        self.session.add(instrument)
        self.session.flush()
        return instrument

    def get_active_by_symbol(self, symbol: str) -> Instrument | None:
        return self.session.scalar(
            select(Instrument).where(Instrument.symbol == symbol, Instrument.retired_at.is_(None))
        )

    def retire(self, instrument: Instrument, *, at: datetime | None = None) -> Instrument:
        instrument.retired_at = at or datetime.now(UTC)
        self.session.flush()
        return instrument


class StrategyRegistryRepository:
    def __init__(self, session: Session):
        self.session = session

    def add_version(self, strategy: StrategyRegistry) -> StrategyRegistry:
        self.session.add(strategy)
        self.session.flush()
        return strategy

    def get_version(self, strategy_id: str, version_tag: str) -> StrategyRegistry | None:
        return self.session.get(StrategyRegistry, (strategy_id, version_tag))

    def retire_version(self, strategy: StrategyRegistry, *, at: datetime | None = None) -> StrategyRegistry:
        strategy.status = "RETIRED"
        strategy.retired_at = at or datetime.now(UTC)
        self.session.flush()
        return strategy
