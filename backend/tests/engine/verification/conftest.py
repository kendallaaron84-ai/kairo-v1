from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from app.db.models.broker import BrokerAccount, BrokerInstrumentCapability
from app.db.models.configuration import Instrument, StrategyRegistry
from app.db.models.ledger import (
    BrokerCashSnapshot,
    Fill,
    KairoCapitalAuthorizationRecord,
    KairoOrder,
    MarketSnapshot,
    OrderIntent,
    OrderObservation,
    RiskDecision,
)
from app.db.models.projections import CapitalCell, CurrentPosition
from app.db.models.risk import (
    RiskGovernorState,
    RiskInstrumentMark,
    RiskSession,
    RiskStateEvent,
)
from app.domain.enums import OptionRight
from engine.execution.replay_orchestrator import (
    LegacyReplayInput,
    ReplayOptionCandidate,
    ReplayOptionChainEvent,
    ReplayOrchestrator,
    ReplaySessionConfig,
    ResearchReplayInput,
)
from engine.risk.governor import RiskGovernor
from engine.risk.models import (
    ExecutionEnvironment,
    InstrumentRiskProfile,
    IntentEvaluationInput,
    MarketMark,
    PositionSnapshot,
    RiskEvaluationRequest,
    RiskSessionSpec,
    StrategyClearance,
)
from engine.strategy.market_data import (
    LegacyReplayProvenance,
    LegacyReplayProvider,
    ResearchEventKind,
    ResearchMarketEvent,
    ResearchReplayProvider,
    SampledPriceObservation,
)


EASTERN = ZoneInfo("America/New_York")
BASE_DATE = date(2026, 9, 1)
BROKER_ID = UUID("50000000-0000-4000-8000-000000000001")
CELL_ID = UUID("50000000-0000-4000-8000-000000000002")
UNDERLYING_ID = UUID("50000000-0000-4000-8000-000000000003")
CALL_ID = UUID("50000000-0000-4000-8000-000000000004")
PUT_ID = UUID("50000000-0000-4000-8000-000000000005")


@dataclass
class VerificationReplaySupport:
    session: Session
    broker: BrokerAccount
    cell: CapitalCell
    underlying: Instrument
    call: Instrument
    put: Instrument

    def session_open(self, day: int = 0) -> datetime:
        target = BASE_DATE + timedelta(days=day)
        return datetime(target.year, target.month, target.day, 9, 30, tzinfo=EASTERN)

    def config(
        self,
        *,
        session_id: str = "VERIFY-SESSION-0",
        day: int = 0,
        authorized: bool = True,
    ) -> ReplaySessionConfig:
        start = self.session_open(day)
        return ReplaySessionConfig(
            session_id=session_id,
            cell_id=self.cell.cell_id,
            broker_account_id=self.broker.broker_account_id,
            session_open=start,
            session_close=start.replace(hour=16),
            execution_authorized_for_replay=authorized,
            initial_cash_usd=Decimal("1000"),
        )

    def provider(self, *, day: int = 0) -> LegacyReplayProvider:
        return LegacyReplayProvider(
            source_id=f"verification-samples-{day}",
            provenance=LegacyReplayProvenance.EXACT_OBSERVED_SAMPLES,
            instrument_id=self.underlying.instrument_id,
            symbol="TQQQ",
        )

    def observations(
        self,
        *,
        day: int = 0,
        prices: tuple[Decimal, ...] | None = None,
    ) -> tuple[SampledPriceObservation, ...]:
        values = prices or tuple([Decimal("10")] * 9 + [Decimal("11"), Decimal("11")])
        start = self.session_open(day)
        return tuple(
            SampledPriceObservation(
                timestamp=start + timedelta(minutes=index),
                price=price,
                instrument_id=self.underlying.instrument_id,
                symbol="TQQQ",
            )
            for index, price in enumerate(values)
        )

    @staticmethod
    def candidate(
        instrument: Instrument,
        *,
        bid: str = "0.47",
        ask: str = "0.50",
    ) -> ReplayOptionCandidate:
        return ReplayOptionCandidate(
            instrument_id=instrument.instrument_id,
            underlying_symbol=instrument.underlying_symbol,
            expiration_date=instrument.expiration_date,
            strike_price=instrument.strike_price,
            option_right=OptionRight(instrument.option_right),
            contract_symbol=instrument.contract_symbol,
            contract_multiplier=instrument.contract_multiplier,
            listing_type=instrument.listing_type,
            bid=Decimal(bid),
            ask=Decimal(ask),
            bid_size=Decimal("10"),
            ask_size=Decimal("10"),
            volume=10,
            open_interest=0,
        )

    def chain(
        self,
        timestamp: datetime,
        *,
        call_bid: str = "0.47",
        call_ask: str = "0.50",
        candidates: tuple[ReplayOptionCandidate, ...] | None = None,
    ) -> ReplayOptionChainEvent:
        return ReplayOptionChainEvent(
            timestamp=timestamp,
            underlying_symbol="TQQQ",
            candidates=candidates
            or (
                self.candidate(self.call, bid=call_bid, ask=call_ask),
                self.candidate(self.put),
            ),
        )

    def legacy_stream(
        self,
        *,
        day: int = 0,
        observations: tuple[SampledPriceObservation, ...] | None = None,
        with_chains: bool = True,
    ) -> LegacyReplayInput:
        source = observations or self.observations(day=day)
        return LegacyReplayInput(
            provider=self.provider(day=day),
            observations=source,
            option_chains=(
                tuple(self.chain(item.timestamp) for item in source)
                if with_chains
                else ()
            ),
        )

    def run_entry(self) -> tuple[ReplayOrchestrator, object]:
        orchestrator = ReplayOrchestrator(self.session, self.config())
        return orchestrator, orchestrator.replay_legacy((self.legacy_stream(),))

    def close_position(self, orchestrator: ReplayOrchestrator) -> None:
        timestamp = self.session_open() + timedelta(minutes=11)
        event = ResearchMarketEvent(
            timestamp=timestamp,
            kind=ResearchEventKind.BAR,
            instrument_id=self.underlying.instrument_id,
            symbol="TQQQ",
            open=Decimal("11"),
            high=Decimal("11"),
            low=Decimal("11"),
            close=Decimal("11"),
        )
        stream = ResearchReplayInput(
            provider=ResearchReplayProvider(
                source_id="verification-close-bar",
                source_kind="CANONICAL_HISTORICAL_BAR",
                instrument_id=self.underlying.instrument_id,
                symbol="TQQQ",
            ),
            events=(event,),
            option_chains=(self.chain(timestamp, call_bid="0.55", call_ask="0.56"),),
        )
        orchestrator.replay_research((stream,))

    def clean_replay_facts(self) -> None:
        # Transactional test reset; production UPDATE/DELETE triggers remain enforced.
        self.session.execute(text("TRUNCATE TABLE fill_realized_pnl"))
        for model in (
            Fill,
            OrderObservation,
            KairoOrder,
            RiskDecision,
            OrderIntent,
            CurrentPosition,
            RiskInstrumentMark,
            RiskStateEvent,
            RiskGovernorState,
            RiskSession,
            MarketSnapshot,
        ):
            self.session.execute(delete(model))
        self.session.flush()
        self.session.expire_all()

    def initialize_governor(
        self,
        *,
        session_id: str = "VERIFY-RISK-SESSION",
        day: int = 0,
        armed: bool = True,
    ) -> RiskGovernor:
        governor = RiskGovernor(self.session, cell_id=self.cell.cell_id)
        start = self.session_open(day)
        governor.initialize_session(
            RiskSessionSpec(
                session_id=session_id,
                trading_date=start.date(),
                session_open=start,
                session_close=start.replace(hour=16),
            )
        )
        if armed:
            governor.arm(authorized_cash_usd=Decimal("1000"))
        return governor

    def risk_request(
        self,
        *,
        purpose: str = "ENTRY",
        side: str = "BUY",
        quantity: Decimal = Decimal("1"),
        age_seconds: Decimal = Decimal("0.2"),
        position: CurrentPosition | None = None,
    ) -> RiskEvaluationRequest:
        intent = OrderIntent(
            intent_id=uuid4(),
            cell_id=self.cell.cell_id,
            strategy_id=self.cell.strategy_id,
            strategy_version=self.cell.strategy_version,
            instrument_id=self.call.instrument_id,
            client_order_key=f"verification-{uuid4()}",
            order_purpose=purpose,
            side=side,
            target_quantity=quantity,
            order_type="MARKET",
        )
        self.session.add(intent)
        self.session.flush()
        now = self.session_open() + timedelta(hours=1)
        return RiskEvaluationRequest(
            intent=IntentEvaluationInput(
                intent_id=intent.intent_id,
                cell_id=self.cell.cell_id,
                strategy_id=self.cell.strategy_id,
                strategy_version=self.cell.strategy_version,
                instrument_id=self.call.instrument_id,
                order_purpose=purpose,
                side=side,
                target_quantity=quantity,
                order_type="MARKET",
            ),
            broker_account_id=self.broker.broker_account_id,
            instrument=InstrumentRiskProfile(
                instrument_id=self.call.instrument_id,
                asset_class="OPTION",
                contract_multiplier=self.call.contract_multiplier,
            ),
            capability=None,
            current_position=(
                PositionSnapshot(
                    position_id=position.position_id,
                    cell_id=position.cell_id,
                    broker_account_id=position.broker_account_id,
                    instrument_id=position.instrument_id,
                    quantity=position.quantity,
                    average_price=position.average_price,
                    contract_multiplier=self.call.contract_multiplier,
                )
                if position is not None
                else None
            ),
            market_mark=MarketMark(
                instrument_id=self.call.instrument_id,
                mark_price=Decimal("0.50"),
                source_timestamp=now - timedelta(seconds=float(age_seconds)),
                received_at=now,
            ),
            strategy_clearance=StrategyClearance.PAPER_ONLY,
            execution_environment=ExecutionEnvironment.PAPER,
            authorized_trading_cash=Decimal("1000"),
            authorized_exposure_usd=Decimal("1000"),
            current_exposure_usd=(
                position.quantity * Decimal("50") if position is not None else Decimal("0")
            ),
        )


@pytest.fixture
def verification_replay(db_session: Session) -> VerificationReplaySupport:
    broker = BrokerAccount(
        broker_account_id=BROKER_ID,
        account_key="verification-paper",
        broker_name="PAPER_SIM_001",
        environment="PAPER",
        status="ACTIVE",
        effective_from=datetime(2026, 9, 1, tzinfo=EASTERN),
    )
    underlying = Instrument(
        instrument_id=UNDERLYING_ID,
        symbol="TQQQ",
        asset_class="EQUITY",
        currency="USD",
        effective_from=datetime(2026, 9, 1, tzinfo=EASTERN),
    )
    call = _option(CALL_ID, "CALL")
    put = _option(PUT_ID, "PUT")
    db_session.add_all([broker, underlying, call, put])
    db_session.flush()
    strategy = db_session.get(StrategyRegistry, ("EMA-CROSS-001", "1.0.0"))
    assert strategy is not None
    cell = CapitalCell(
        cell_id=CELL_ID,
        cell_code="VERIFY-CELL",
        seed_capital=Decimal("1000"),
        status="ACTIVE",
        autonomy_tier="APPRENTICE",
        strategy_id=strategy.strategy_id,
        strategy_version=strategy.version_tag,
        target_treasury_code="META",
        updated_at=datetime(2026, 9, 1, tzinfo=EASTERN),
    )
    db_session.add(cell)
    db_session.flush()
    for instrument in (call, put):
        db_session.add(
            BrokerInstrumentCapability(
                capability_id=UUID(int=instrument.instrument_id.int + 100),
                broker_account_id=broker.broker_account_id,
                instrument_id=instrument.instrument_id,
                can_trade=True,
                can_fractional=False,
                can_short=False,
                notional_orders_supported=False,
                options_supported=True,
                extended_hours_supported=False,
                minimum_quantity=Decimal("1"),
                effective_from=datetime(2026, 9, 1, tzinfo=EASTERN),
            )
        )
    cash = BrokerCashSnapshot(
        snapshot_id=UUID("50000000-0000-4000-8000-000000000006"),
        broker_account_id=broker.broker_account_id,
        broker_cash=Decimal("1000"),
        settled_cash=Decimal("1000"),
        unsettled_cash=Decimal("0"),
        buying_power=Decimal("1000"),
        currency="USD",
        captured_at=datetime(2026, 9, 1, tzinfo=EASTERN),
    )
    db_session.add(cash)
    db_session.flush()
    db_session.add(
        KairoCapitalAuthorizationRecord(
            authorization_id=UUID("50000000-0000-4000-8000-000000000007"),
            cell_id=cell.cell_id,
            broker_snapshot_id=cash.snapshot_id,
            broker_account_id=broker.broker_account_id,
            settled_cash=Decimal("1000"),
            safety_reserve=Decimal("0"),
            ownership_treasury_reserved=Decimal("0"),
            replication_reserve=Decimal("0"),
            committed_obligations=Decimal("0"),
            authorized_trading_cash=Decimal("1000"),
            computed_at=datetime(2026, 9, 1, tzinfo=EASTERN),
        )
    )
    db_session.flush()
    return VerificationReplaySupport(db_session, broker, cell, underlying, call, put)


def _option(instrument_id: UUID, right: str) -> Instrument:
    return Instrument(
        instrument_id=instrument_id,
        symbol=f"TQQQ-{right}-VERIFY",
        asset_class="OPTION",
        currency="USD",
        underlying_symbol="TQQQ",
        contract_symbol=f"TQQQ260901{right[0]}00010000",
        expiration_date=BASE_DATE,
        strike_price=Decimal("10"),
        option_right=right,
        contract_multiplier=Decimal("100"),
        listing_type="STANDARD",
        effective_from=datetime(2026, 9, 1, tzinfo=EASTERN),
    )
