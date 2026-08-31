import asyncio
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.configuration import Instrument
from app.db.models.ledger import (
    Fill,
    KairoOrder,
    MarketSnapshot,
    OrderIntent,
    OrderObservation,
    RiskDecision,
)
from app.db.models.projections import CurrentPosition
from app.db.models.risk import (
    RiskGovernorState,
    RiskInstrumentMark,
    RiskSession,
    RiskStateEvent,
)
from app.domain.enums import OptionRight
from engine.execution.models import (
    ExecutionQuote,
    LiquidityFidelityTier,
    PaperEngineConfig,
)
from engine.execution.paper_broker import PaperExecutionEngine
from engine.execution.virtual_clock import ReplayIdentityFactory, VirtualClock
from engine.risk.governor import RiskGovernor
from engine.risk.pnl_tracker import realized_round_trip_pnl
from engine.risk.models import (
    DecisionVerdict,
    ExecutionEnvironment,
    FillAccountingEvent,
    InstrumentRiskProfile,
    IntentEvaluationInput,
    MarketMark,
    PositionSnapshot,
    RiskEvaluationRequest,
    RiskSessionSpec,
    StrategyClearance,
)
from engine.strategy.ema_cross_strategy import (
    EMACrossStrategy,
    StrategyContract,
    StrategyOrderSignal,
    StrategyPosition,
)


class ReplaySessionConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    cell_id: UUID
    broker_account_id: UUID
    session_open: datetime
    session_close: datetime
    strategy_id: str = "EMA-CROSS-001"
    strategy_version: str = "1.0.0"
    environment: str = "PAPER"
    initial_cash_usd: Decimal = Field(default=Decimal("100.00"), ge=0)


class ReplayOptionQuote(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_id: UUID
    option_right: OptionRight
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)
    bid_size: Decimal = Field(gt=0)
    ask_size: Decimal = Field(gt=0)


class ReplayMarketEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_id: UUID
    symbol: str
    timestamp: datetime
    price: Decimal = Field(gt=0)
    completed_minute_close: bool = True
    call_quote: ReplayOptionQuote | None = None
    put_quote: ReplayOptionQuote | None = None


class ReplayRunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest_hash: str
    financial_ids: tuple[UUID, ...]
    event_count: int


class ReplayOrchestrator:
    def __init__(self, session: Session, config: ReplaySessionConfig) -> None:
        if config.session_open.tzinfo is None or config.session_close.tzinfo is None:
            raise ValueError("replay session timestamps must be timezone-aware")
        self.session = session
        self.config = config
        self.clock = VirtualClock(config.session_open)
        self.identities = ReplayIdentityFactory(config.session_id)
        self.governor = RiskGovernor(
            session, clock=self.clock, identities=self.identities
        )
        self.paper = PaperExecutionEngine(
            session,
            PaperEngineConfig(broker_account_id=config.broker_account_id),
            clock=self.clock,
            identities=self.identities,
        )
        self.strategy = EMACrossStrategy(settled_cash=config.initial_cash_usd)
        self._snapshot_ids: list[UUID] = []
        self._intent_ids: list[UUID] = []
        self._event_count = 0

    def initialize(self) -> None:
        state = self.governor.initialize_session(
            RiskSessionSpec(
                session_id=self.config.session_id,
                trading_date=self.config.session_open.date(),
                session_open=self.config.session_open,
                session_close=self.config.session_close,
            )
        )
        if state.operational_state == "DISARMED":
            self.governor.arm(authorized_cash_usd=self.config.initial_cash_usd)

    def replay(self, events: tuple[ReplayMarketEvent, ...]) -> ReplayRunResult:
        self.initialize()
        for event in events:
            self.process_event(event)
        manifest_hash, ids = self.build_manifest()
        return ReplayRunResult(
            manifest_hash=manifest_hash,
            financial_ids=ids,
            event_count=self._event_count,
        )

    def process_event(self, event: ReplayMarketEvent) -> None:
        if event.timestamp.tzinfo is None or event.timestamp.utcoffset() is None:
            raise ValueError("replay market event timestamp must be timezone-aware")
        self.clock.advance_to(event.timestamp)
        self._event_count += 1
        self._persist_underlying_snapshot(event)

        option_quotes = tuple(
            item for item in (event.call_quote, event.put_quote) if item is not None
        )
        execution_quotes = {
            item.option_right: self._persist_option_snapshot(item) for item in option_quotes
        }

        # Mark-to-market always precedes indicator and strategy evaluation.
        self._record_marks(event, option_quotes)
        if not event.completed_minute_close:
            return
        contracts = {
            item.option_right: self._strategy_contract(event.symbol, item)
            for item in option_quotes
        }
        position = self.strategy.positions.get(event.symbol)
        position_bid = None
        if position is not None:
            selected = contracts.get(position.option_right)
            position_bid = selected.bid if selected is not None else None
        signal = self.strategy.on_bar(
            symbol=event.symbol,
            close=event.price,
            timestamp=self.clock.now(),
            call_contract=contracts.get(OptionRight.CALL),
            put_contract=contracts.get(OptionRight.PUT),
            position_quote_bid=position_bid,
        )
        if signal is None:
            return
        execution_quote = execution_quotes.get(signal.option_right)
        if execution_quote is None:
            return
        self._route_signal(signal, execution_quote)

    def _persist_underlying_snapshot(self, event: ReplayMarketEvent) -> None:
        snapshot_id = self.identities.generate_id(
            "market_snapshot",
            event.instrument_id,
            self.clock.now(),
            parent_id=f"event:{self._event_count}",
        )
        self.session.add(
            MarketSnapshot(
                snapshot_id=snapshot_id,
                instrument_id=event.instrument_id,
                captured_at=self.clock.now(),
                last=event.price,
                payload={
                    "source": "REPLAY_ORCHESTRATOR",
                    "symbol": event.symbol,
                    "completed_minute_close": event.completed_minute_close,
                },
            )
        )
        self._snapshot_ids.append(snapshot_id)
        self.session.flush()

    def _persist_option_snapshot(
        self, quote: ReplayOptionQuote
    ) -> ExecutionQuote:
        snapshot_id = self.identities.generate_id(
            "market_snapshot",
            quote.instrument_id,
            self.clock.now(),
            parent_id=f"event:{self._event_count}",
        )
        self.session.add(
            MarketSnapshot(
                snapshot_id=snapshot_id,
                instrument_id=quote.instrument_id,
                captured_at=self.clock.now(),
                bid=quote.bid,
                ask=quote.ask,
                last=(quote.bid + quote.ask) / Decimal("2"),
                payload={
                    "source": "REPLAY_ORCHESTRATOR",
                    "bid_size": str(quote.bid_size),
                    "ask_size": str(quote.ask_size),
                },
            )
        )
        self._snapshot_ids.append(snapshot_id)
        self.session.flush()
        return ExecutionQuote(
            snapshot_id=snapshot_id,
            instrument_id=quote.instrument_id,
            bid=quote.bid,
            ask=quote.ask,
            bid_size=quote.bid_size,
            ask_size=quote.ask_size,
            captured_at=self.clock.now(),
            fidelity_tier=LiquidityFidelityTier.TIER_1_QUOTE_DEPTH,
        )

    def _record_marks(
        self,
        event: ReplayMarketEvent,
        option_quotes: tuple[ReplayOptionQuote, ...],
    ) -> None:
        marks = [(event.instrument_id, event.price)] + [
            (item.instrument_id, (item.bid + item.ask) / Decimal("2"))
            for item in option_quotes
        ]
        for instrument_id, price in marks:
            self.governor.record_market_mark(
                MarketMark(
                    instrument_id=instrument_id,
                    mark_price=price,
                    source_timestamp=self.clock.now(),
                    received_at=self.clock.now(),
                ),
                positions=self._position_snapshots(),
                authorized_cash_usd=self.config.initial_cash_usd,
            )

    def _strategy_contract(
        self, symbol: str, quote: ReplayOptionQuote
    ) -> StrategyContract:
        instrument = self.session.get(Instrument, quote.instrument_id)
        if instrument is None or instrument.contract_multiplier is None:
            raise ValueError("replay option lacks canonical instrument identity")
        return StrategyContract(
            instrument_id=instrument.instrument_id,
            underlying_symbol=symbol,
            option_right=quote.option_right,
            bid=quote.bid,
            ask=quote.ask,
            contract_multiplier=instrument.contract_multiplier,
        )

    def _route_signal(
        self, signal: StrategyOrderSignal, quote: ExecutionQuote
    ) -> None:
        intent_id = self.identities.generate_id(
            "order_intent",
            signal.instrument_id,
            self.clock.now(),
            parent_id=f"strategy:{signal.underlying_symbol}",
        )
        intent = OrderIntent(
            intent_id=intent_id,
            cell_id=self.config.cell_id,
            strategy_id=self.config.strategy_id,
            strategy_version=self.config.strategy_version,
            instrument_id=signal.instrument_id,
            client_order_key=f"replay:{self.config.session_id}:{intent_id}",
            order_purpose=signal.order_purpose.value,
            side=signal.side.value,
            target_quantity=signal.quantity,
            order_type="LIMIT",
            limit_price=signal.limit_price,
            created_at=self.clock.now(),
        )
        self.session.add(intent)
        self._intent_ids.append(intent_id)
        self.session.flush()
        mark_price = quote.ask if signal.side.value == "BUY" else quote.bid
        instrument = self.session.get(Instrument, signal.instrument_id)
        if instrument is None:
            raise ValueError("strategy signal lacks canonical instrument identity")
        position = self._position_for_instrument(signal.instrument_id)
        result = self.governor.evaluate(
            RiskEvaluationRequest(
                intent=IntentEvaluationInput(
                    intent_id=intent_id,
                    cell_id=self.config.cell_id,
                    strategy_id=self.config.strategy_id,
                    strategy_version=self.config.strategy_version,
                    instrument_id=signal.instrument_id,
                    order_purpose=signal.order_purpose,
                    side=signal.side,
                    target_quantity=signal.quantity,
                    order_type="LIMIT",
                ),
                broker_account_id=self.config.broker_account_id,
                instrument=InstrumentRiskProfile(
                    instrument_id=signal.instrument_id,
                    asset_class=instrument.asset_class,
                    contract_multiplier=instrument.contract_multiplier,
                ),
                capability=None,
                current_position=self._snapshot(position, instrument),
                market_mark=MarketMark(
                    instrument_id=signal.instrument_id,
                    mark_price=mark_price,
                    source_timestamp=self.clock.now(),
                    received_at=self.clock.now(),
                ),
                strategy_clearance=StrategyClearance.PAPER_ONLY,
                execution_environment=ExecutionEnvironment.PAPER,
                authorized_trading_cash=self.config.initial_cash_usd,
                authorized_exposure_usd=self.config.initial_cash_usd,
                current_exposure_usd=Decimal("0"),
            )
        )
        if result.verdict is not DecisionVerdict.AUTHORIZED:
            return
        order_id = self.identities.generate_id(
            "kairo_order",
            signal.instrument_id,
            self.clock.now(),
            parent_id=intent_id,
        )
        self.session.add(
            KairoOrder(
                kairo_order_id=order_id,
                intent_id=intent_id,
                risk_decision_id=result.decision_id,
                broker_account_id=self.config.broker_account_id,
                status="PENDING_SUBMIT",
                submitted_at=self.clock.now(),
            )
        )
        self.session.flush()
        receipt = asyncio.run(self.paper.submit_order(order_id, quote))
        if receipt.fill_records:
            self._apply_fills(signal.underlying_symbol, receipt.fill_records)

    def _apply_fills(self, underlying_symbol: str, fills) -> None:
        for fill in fills:
            position = self._position_for_instrument(fill.instrument_id)
            realized = Decimal("0")
            if fill.side == "BUY":
                if position is None:
                    position = CurrentPosition(
                        position_id=self.identities.generate_id(
                            "current_position",
                            fill.instrument_id,
                            self.clock.now(),
                            parent_id=fill.fill_id,
                        ),
                        cell_id=self.config.cell_id,
                        broker_account_id=self.config.broker_account_id,
                        instrument_id=fill.instrument_id,
                        quantity=fill.quantity,
                        average_price=fill.fill_price,
                        updated_at=self.clock.now(),
                    )
                    self.session.add(position)
                else:
                    total = position.quantity + fill.quantity
                    position.average_price = (
                        position.average_price * position.quantity
                        + fill.fill_price * fill.quantity
                    ) / total
                    position.quantity = total
                    position.updated_at = self.clock.now()
                instrument = self.session.get(Instrument, fill.instrument_id)
                self.strategy.record_open(
                    StrategyPosition(
                        instrument_id=fill.instrument_id,
                        underlying_symbol=underlying_symbol,
                        option_right=OptionRight(instrument.option_right),
                        quantity=fill.quantity,
                        entry_price=fill.fill_price,
                        contract_multiplier=fill.contract_multiplier,
                    )
                )
            elif position is not None:
                realized = realized_round_trip_pnl(
                    entry_price=position.average_price,
                    exit_price=fill.fill_price,
                    quantity=fill.quantity,
                    contract_multiplier=fill.contract_multiplier,
                    position_side="LONG",
                )
                position.quantity -= fill.quantity
                position.updated_at = self.clock.now()
                self.strategy.record_close(
                    underlying_symbol, realized_pnl=realized
                )
            self.session.flush()
            # Effective fill prices already include modeled slippage. Re-mark the
            # canonical portfolio after the position mutation so a closed position
            # cannot leave stale unrealized P&L in the governor state.
            self.governor.record_market_mark(
                MarketMark(
                    instrument_id=fill.instrument_id,
                    mark_price=fill.fill_price,
                    source_timestamp=self.clock.now(),
                    received_at=self.clock.now(),
                ),
                positions=self._position_snapshots(),
                authorized_cash_usd=self.config.initial_cash_usd,
            )
            self.governor.record_fill_accounting(
                FillAccountingEvent(
                    fill_id=fill.fill_id,
                    kairo_order_id=fill.kairo_order_id,
                    broker_account_id=fill.broker_account_id,
                    instrument_id=fill.instrument_id,
                    realized_pnl_delta_usd=realized,
                    commission_fees_usd=fill.commission_fee_usd,
                    slippage_usd=fill.slippage_usd,
                    fill_price=fill.fill_price,
                    filled_qty=fill.quantity,
                    timestamp=self.clock.now(),
                ),
                authorized_cash_usd=self.config.initial_cash_usd,
                open_positions=self._position_snapshots(),
            )

    def _position_for_instrument(self, instrument_id: UUID) -> CurrentPosition | None:
        return self.session.scalar(
            select(CurrentPosition).where(
                CurrentPosition.cell_id == self.config.cell_id,
                CurrentPosition.broker_account_id == self.config.broker_account_id,
                CurrentPosition.instrument_id == instrument_id,
                CurrentPosition.quantity != 0,
            )
        )

    def _position_snapshots(self) -> list[PositionSnapshot]:
        rows = self.session.execute(
            select(CurrentPosition, Instrument)
            .join(Instrument, Instrument.instrument_id == CurrentPosition.instrument_id)
            .where(CurrentPosition.quantity != 0)
        ).all()
        return [self._snapshot(position, instrument) for position, instrument in rows]

    @staticmethod
    def _snapshot(
        position: CurrentPosition | None, instrument: Instrument
    ) -> PositionSnapshot | None:
        if position is None:
            return None
        return PositionSnapshot(
            position_id=position.position_id,
            cell_id=position.cell_id,
            broker_account_id=position.broker_account_id,
            instrument_id=position.instrument_id,
            quantity=position.quantity,
            average_price=position.average_price,
            contract_multiplier=instrument.contract_multiplier or Decimal("1"),
        )

    def build_manifest(self) -> tuple[str, tuple[UUID, ...]]:
        models_and_rows = [
            (
                "market_snapshots",
                [self.session.get(MarketSnapshot, item) for item in self._snapshot_ids],
            ),
            (
                "order_intents",
                [self.session.get(OrderIntent, item) for item in self._intent_ids],
            ),
        ]
        decisions = list(
            self.session.scalars(
                select(RiskDecision).where(RiskDecision.session_id == self.config.session_id)
            )
        )
        orders = list(
            self.session.scalars(
                select(KairoOrder).where(KairoOrder.intent_id.in_(self._intent_ids))
            )
        ) if self._intent_ids else []
        order_ids = [item.kairo_order_id for item in orders]
        models_and_rows.extend(
            [
                ("risk_decisions", decisions),
                ("kairo_orders", orders),
                (
                    "order_observations",
                    list(
                        self.session.scalars(
                            select(OrderObservation).where(
                                OrderObservation.kairo_order_id.in_(order_ids)
                            )
                        )
                    )
                    if order_ids
                    else [],
                ),
                (
                    "fills",
                    list(
                        self.session.scalars(
                            select(Fill).where(Fill.kairo_order_id.in_(order_ids))
                        )
                    )
                    if order_ids
                    else [],
                ),
                (
                    "risk_state_events",
                    list(
                        self.session.scalars(
                            select(RiskStateEvent).where(
                                RiskStateEvent.session_id == self.config.session_id
                            )
                        )
                    ),
                ),
                (
                    "risk_instrument_marks",
                    list(
                        self.session.scalars(
                            select(RiskInstrumentMark).where(
                                RiskInstrumentMark.session_id
                                == self.config.session_id
                            )
                        )
                    ),
                ),
                (
                    "current_positions",
                    list(
                        self.session.scalars(
                            select(CurrentPosition).where(
                                CurrentPosition.cell_id == self.config.cell_id
                            )
                        )
                    ),
                ),
                (
                    "risk_sessions",
                    [self.session.get(RiskSession, self.config.session_id)],
                ),
                ("risk_governor_state", [self.session.get(RiskGovernorState, 1)]),
            ]
        )
        manifest: list[dict[str, Any]] = []
        financial_ids: list[UUID] = []
        for table, rows in models_and_rows:
            for row in rows:
                if row is None:
                    continue
                values = {
                    column.name: _json_value(getattr(row, column.name))
                    for column in row.__table__.columns
                }
                manifest.append({"table": table, "values": values})
                for column in row.__table__.primary_key.columns:
                    value = getattr(row, column.name)
                    if isinstance(value, UUID):
                        financial_ids.append(value)
        manifest.sort(
            key=lambda item: (
                item["table"],
                json.dumps(item["values"], sort_keys=True),
            )
        )
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest(), tuple(sorted(set(financial_ids), key=str))


def _json_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value
