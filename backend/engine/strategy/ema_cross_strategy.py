from datetime import datetime, time
from decimal import Decimal, ROUND_FLOOR
from enum import StrEnum
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import OptionRight, OrderPurpose, OrderSide
from engine.strategy.indicators import PrototypeEMA9


EASTERN = ZoneInfo("America/New_York")


class StrategySignalReason(StrEnum):
    BULLISH_CROSS = "BULLISH_CROSS"
    BEARISH_CROSS = "BEARISH_CROSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    TREND_REVERSAL = "TREND_REVERSAL"
    FORCED_FLATTEN = "FORCED_FLATTEN"


class StrategyContract(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_id: UUID
    underlying_symbol: str
    option_right: OptionRight
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)
    contract_multiplier: Decimal = Field(gt=0)


class StrategyPosition(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_id: UUID
    underlying_symbol: str
    option_right: OptionRight
    quantity: Decimal = Field(gt=0)
    entry_price: Decimal = Field(gt=0)
    contract_multiplier: Decimal = Field(gt=0)


class StrategyOrderSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    underlying_symbol: str
    instrument_id: UUID
    option_right: OptionRight
    side: OrderSide
    quantity: Decimal = Field(gt=0)
    limit_price: Decimal = Field(gt=0)
    order_purpose: OrderPurpose
    reason: StrategySignalReason
    emitted_at: datetime


class EMACrossStrategy:
    """Frozen EMA-CROSS-001 v1.0.0 runtime; no research variants."""

    def __init__(self, *, settled_cash: Decimal) -> None:
        if settled_cash < 0:
            raise ValueError("settled cash cannot be negative")
        self.daily_budget = settled_cash * Decimal("0.50")
        self.slot_size = self.daily_budget / Decimal("3")
        self._indicators = {symbol: PrototypeEMA9() for symbol in ("TQQQ", "SQQQ")}
        self._positions: dict[str, StrategyPosition] = {}
        self.consecutive_losses = 0
        self.entries_halted = False
        self.last_missing_execution_evidence: str | None = None

    @property
    def positions(self) -> dict[str, StrategyPosition]:
        return dict(self._positions)

    def record_open(self, position: StrategyPosition) -> None:
        if position.underlying_symbol in self._positions:
            raise ValueError("prototype allows one position per underlying")
        self._positions[position.underlying_symbol] = position

    def record_close(self, underlying_symbol: str, *, realized_pnl: Decimal) -> None:
        self._positions.pop(underlying_symbol, None)
        if realized_pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= 2:
                self.entries_halted = True
        else:
            self.consecutive_losses = 0

    def on_bar(
        self,
        *,
        symbol: str,
        close: Decimal,
        timestamp: datetime,
        call_contract: StrategyContract | None = None,
        put_contract: StrategyContract | None = None,
        position_quote_bid: Decimal | None = None,
    ) -> StrategyOrderSignal | None:
        self.last_missing_execution_evidence = None
        if symbol not in self._indicators:
            raise ValueError(f"unsupported frozen symbol: {symbol}")
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("strategy bar timestamp must be timezone-aware")
        eastern_time = timestamp.astimezone(EASTERN).time()
        if eastern_time >= time(16, 0):
            return None

        indicator = self._indicators[symbol]
        current = indicator.append(close)
        previous = indicator.values[-2] if len(indicator.values) >= 2 else None
        position = self._positions.get(symbol)
        if position is not None:
            contract = call_contract if position.option_right is OptionRight.CALL else put_contract
            bid = position_quote_bid or (contract.bid if contract is not None else None)
            if bid is None or bid <= 0:
                self.last_missing_execution_evidence = "POSITION_EXIT_QUOTE_MISSING"
                return None
            reason: StrategySignalReason | None = None
            purpose = OrderPurpose.EMERGENCY_EXIT
            if eastern_time >= time(15, 45):
                reason = StrategySignalReason.FORCED_FLATTEN
            else:
                option_return = (bid - position.entry_price) / position.entry_price
                if option_return >= Decimal("0.10"):
                    reason = StrategySignalReason.TAKE_PROFIT
                    purpose = OrderPurpose.TAKE_PROFIT
                elif option_return <= Decimal("-0.05"):
                    reason = StrategySignalReason.STOP_LOSS
                    purpose = OrderPurpose.STOP_LOSS
                elif current.ema is not None and (
                    (position.option_right is OptionRight.CALL and close < current.ema)
                    or (position.option_right is OptionRight.PUT and close > current.ema)
                ):
                    reason = StrategySignalReason.TREND_REVERSAL
            if reason is None:
                return None
            return StrategyOrderSignal(
                underlying_symbol=symbol,
                instrument_id=position.instrument_id,
                option_right=position.option_right,
                side=OrderSide.SELL,
                quantity=position.quantity,
                limit_price=bid,
                order_purpose=purpose,
                reason=reason,
                emitted_at=timestamp,
            )

        if (
            eastern_time >= time(15, 45)
            or self.entries_halted
            or not current.ready
            or previous is None
            or previous.ema is None
            or current.ema is None
        ):
            return None
        if previous.close <= previous.ema and close > current.ema:
            contract = call_contract
            reason = StrategySignalReason.BULLISH_CROSS
        elif previous.close >= previous.ema and close < current.ema:
            contract = put_contract
            reason = StrategySignalReason.BEARISH_CROSS
        else:
            return None
        if contract is None or contract.underlying_symbol != symbol:
            self.last_missing_execution_evidence = "ENTRY_OPTION_QUOTE_MISSING"
            return None
        quantity = (
            self.slot_size / (contract.ask * contract.contract_multiplier)
        ).to_integral_value(rounding=ROUND_FLOOR)
        if quantity < 1:
            return None
        return StrategyOrderSignal(
            underlying_symbol=symbol,
            instrument_id=contract.instrument_id,
            option_right=contract.option_right,
            side=OrderSide.BUY,
            quantity=quantity,
            limit_price=contract.ask,
            order_purpose=OrderPurpose.ENTRY,
            reason=reason,
            emitted_at=timestamp,
        )
