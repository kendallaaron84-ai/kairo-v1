"""Deterministic, synthetic-only Strategy 001 research simulator.

This module has no provider, database, broker, or canonical-dataset integration.
Real Q1 execution remains outside its authority boundary.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator


POLICY_ID = "RESEARCH-POLICY-v1.1"
POLICY_GIT_COMMIT = "90692550708a2755e2d3d5db1151c9af91857580"
POLICY_SHA256 = "eaccb5b21f992148f07f2547d6729beed6beead5ee38e24dc45500518bb02718"
DATASET_ID = "ab8036fc-87cb-5a5b-abaf-38c704f68ddd"
QUALIFICATION = "FAIL — 41.58%"
CANDIDATE_DIAGNOSTIC = "99.99%"
INFERENCE_BOUNDARY = (
    "Performance conditional on the subset of options captured by the static window."
)
POLICY_PATH = Path(__file__).resolve().parents[2] / "docs" / "research-policy-v1.1.md"
EASTERN = ZoneInfo("America/New_York")
CONTRACT_MULTIPLIER = Decimal("100")
ENTRY_TARGET = Decimal("0.35")
ONE_TICK = Decimal("0.01")
CENT = Decimal("0.01")
PERCENT = Decimal("0.01")
CAPITAL_TIERS = (
    Decimal("100.00"),
    Decimal("250.00"),
    Decimal("500.00"),
    Decimal("1000.00"),
    Decimal("1500.00"),
    Decimal("2500.00"),
)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT)


def _percentage(numerator: Decimal | int, denominator: Decimal | int) -> Decimal:
    denominator_value = Decimal(denominator)
    if denominator_value == 0:
        return Decimal("0.00")
    return (Decimal(numerator) / denominator_value * Decimal("100")).quantize(PERCENT)


def _aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


class ExecutionMode(StrEnum):
    BASELINE = "BASELINE"
    STRESS_1_TICK = "STRESS_1_TICK"


class SignalKind(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class SignalDisposition(StrEnum):
    EXECUTED = "EXECUTED"
    CANDIDATE_UNAVAILABLE_SKIP = "CANDIDATE_UNAVAILABLE_SKIP"
    SKIPPED_INSUFFICIENT_FUNDS = "SKIPPED_INSUFFICIENT_FUNDS"
    ACTIVE_POSITION_SUPPRESSION = "ACTIVE_POSITION_SUPPRESSION"


class ExitReason(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    PROFIT_TARGET = "PROFIT_TARGET"
    SIGNAL_REVERSAL = "SIGNAL_REVERSAL"
    SESSION_FORCE_CLOSE = "SESSION_FORCE_CLOSE"


class UnderlyingBar(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str = Field(pattern=r"^(SQQQ|TQQQ)$")
    interval_start_at: datetime
    completed_at: datetime
    close: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def valid_interval(self) -> "UnderlyingBar":
        _aware(self.interval_start_at, "underlying interval start")
        _aware(self.completed_at, "underlying completion")
        if self.completed_at <= self.interval_start_at:
            raise ValueError("underlying interval must complete after it starts")
        if self.completed_at - self.interval_start_at != timedelta(minutes=1):
            raise ValueError("underlying interval must span exactly one minute")
        return self


class OptionCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_id: str = Field(min_length=1)
    underlying_symbol: str = Field(pattern=r"^(SQQQ|TQQQ)$")
    observed_at: datetime
    expiration: date
    strike: Decimal = Field(gt=0)
    right: str = Field(pattern=r"^(CALL|PUT)$")
    bid: Decimal = Field(ge=0)
    ask: Decimal = Field(gt=0)
    volume: int | None = Field(default=None, ge=0)
    open_interest: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_quote(self) -> "OptionCandidate":
        _aware(self.observed_at, "candidate observation")
        if self.ask < self.bid:
            raise ValueError("candidate ask cannot be below bid")
        return self


class OptionInterval(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_id: str = Field(min_length=1)
    interval_start_at: datetime
    completed_at: datetime
    bid_close: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def valid_interval(self) -> "OptionInterval":
        _aware(self.interval_start_at, "option interval start")
        _aware(self.completed_at, "option interval completion")
        if self.completed_at <= self.interval_start_at:
            raise ValueError("option interval must complete after it starts")
        if self.completed_at - self.interval_start_at != timedelta(minutes=1):
            raise ValueError("option interval must span exactly one minute")
        return self


class ResearchSimulationInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    underlying_bars: tuple[UnderlyingBar, ...]
    option_candidates: tuple[OptionCandidate, ...]
    option_intervals: tuple[OptionInterval, ...]
    stage_4_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    simulator_git_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")

    @model_validator(mode="after")
    def deterministic_inputs(self) -> "ResearchSimulationInput":
        bar_keys = [(item.symbol, item.interval_start_at) for item in self.underlying_bars]
        if len(bar_keys) != len(set(bar_keys)):
            raise ValueError("underlying bars must be unique by symbol and interval")
        candidate_keys = [
            (item.underlying_symbol, item.observed_at, item.instrument_id)
            for item in self.option_candidates
        ]
        if len(candidate_keys) != len(set(candidate_keys)):
            raise ValueError("option candidates must be unique")
        interval_keys = [
            (item.instrument_id, item.interval_start_at) for item in self.option_intervals
        ]
        if len(interval_keys) != len(set(interval_keys)):
            raise ValueError("option intervals must be unique")
        return self


class PolicyBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    policy_id: str
    policy_git_commit: str
    policy_sha256: str
    dataset_id: str
    stage_4_manifest_sha256: str
    simulator_git_commit_sha: str
    q1_qualification: str
    candidate_diagnostic: str
    live_capital_authorization: bool


class SignalLedgerEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    stream: str
    execution_mode: ExecutionMode
    capital_tier: Decimal | None
    symbol: str
    signal_at: datetime
    legal_entry_at: datetime
    disposition: SignalDisposition
    selected_instrument_id: str | None = None
    quantity: int = Field(default=0, ge=0)


class TradeLedgerEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    trade_id: str
    stream: str
    execution_mode: ExecutionMode
    capital_tier: Decimal | None
    underlying: str
    signal_timestamp: datetime
    legal_entry_timestamp: datetime
    selected_option_identity: str
    expiration: date
    strike: Decimal
    right: str
    selection_bid: Decimal
    selection_ask: Decimal
    entry_price: Decimal
    exit_timestamp: datetime
    exit_price: Decimal
    exit_reason: ExitReason
    quantity: int = Field(gt=0)
    gross_pnl: Decimal
    fees: Decimal = Field(ge=0)
    net_pnl: Decimal


class SummaryMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    executed_trade_count: int = Field(ge=0)
    net_expectancy: Decimal
    profit_factor: Decimal | None
    win_rate: Decimal
    maximum_drawdown: Decimal = Field(ge=0)
    candidate_skips: int = Field(ge=0)
    affordability_skips: int = Field(ge=0)
    active_position_suppressions: int = Field(ge=0)
    terminal_equity: Decimal | None


class ModeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    execution_mode: ExecutionMode
    signal_ledger: tuple[SignalLedgerEntry, ...]
    trade_ledger: tuple[TradeLedgerEntry, ...]
    summary: SummaryMetrics


class DualModeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    baseline: ModeResult
    stressed: ModeResult
    baseline_expectancy: Decimal
    stressed_expectancy: Decimal


class CapitalTierResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    starting_capital: Decimal
    allocation_rate: Decimal
    results: DualModeResult


class Strategy001ResearchReceipt(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str
    binding: PolicyBinding
    inference_boundary: str
    strategy_economics: DualModeResult
    capital_feasibility: tuple[CapitalTierResult, ...]

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.model_dump(mode="json"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class _Signal(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    kind: SignalKind
    signal_at: datetime
    legal_execution_at: datetime
    spot_close: Decimal


@dataclass
class _Position:
    signal: _Signal
    candidate: OptionCandidate
    quantity: int
    entry_price: Decimal
    entry_fee: Decimal


@dataclass
class _Account:
    initial_equity: Decimal | None
    cash: Decimal | None
    latest_bid: dict[str, Decimal]


def load_policy_binding(
    *,
    stage_4_manifest_sha256: str,
    simulator_git_commit_sha: str,
    policy_path: Path = POLICY_PATH,
) -> PolicyBinding:
    """Fail closed unless the exact frozen policy bytes are present."""

    content = policy_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != POLICY_SHA256:
        raise ValueError("RESEARCH-POLICY-v1.1 SHA-256 mismatch")
    return PolicyBinding(
        policy_id=POLICY_ID,
        policy_git_commit=POLICY_GIT_COMMIT,
        policy_sha256=digest,
        dataset_id=DATASET_ID,
        stage_4_manifest_sha256=stage_4_manifest_sha256,
        simulator_git_commit_sha=simulator_git_commit_sha,
        q1_qualification=QUALIFICATION,
        candidate_diagnostic=CANDIDATE_DIAGNOSTIC,
        live_capital_authorization=False,
    )


def per_side_fee(quantity: int) -> Decimal:
    if quantity <= 0:
        raise ValueError("fee quantity must be positive")
    return _money(max(Decimal("1.00"), Decimal("0.70") * quantity))


def derive_signals(bars: Iterable[UnderlyingBar]) -> tuple[_Signal, ...]:
    """Derive completed-bar EMA(9/21) crosses and their next legal intervals."""

    by_symbol: dict[str, list[UnderlyingBar]] = defaultdict(list)
    for bar in bars:
        by_symbol[bar.symbol].append(bar)
    signals: list[_Signal] = []
    for symbol in sorted(by_symbol):
        ordered = sorted(by_symbol[symbol], key=lambda item: item.interval_start_at)
        if any(
            current.interval_start_at < previous.completed_at
            for previous, current in zip(ordered, ordered[1:])
        ):
            raise ValueError("underlying intervals overlap")
        fast: Decimal | None = None
        slow: Decimal | None = None
        prior_fast: Decimal | None = None
        prior_slow: Decimal | None = None
        fast_alpha = Decimal("2") / Decimal("10")
        slow_alpha = Decimal("2") / Decimal("22")
        for index, bar in enumerate(ordered):
            fast = bar.close if fast is None else fast_alpha * bar.close + (1 - fast_alpha) * fast
            slow = bar.close if slow is None else slow_alpha * bar.close + (1 - slow_alpha) * slow
            kind: SignalKind | None = None
            if prior_fast is not None and prior_slow is not None:
                if prior_fast <= prior_slow and fast > slow:
                    kind = SignalKind.BULLISH
                elif prior_fast >= prior_slow and fast < slow:
                    kind = SignalKind.BEARISH
            if kind is not None and index + 1 < len(ordered):
                next_bar = ordered[index + 1]
                if next_bar.interval_start_at < bar.completed_at:
                    raise ValueError("legal execution interval precedes signal completion")
                signals.append(_Signal(
                    symbol=symbol,
                    kind=kind,
                    signal_at=bar.completed_at,
                    legal_execution_at=next_bar.interval_start_at,
                    spot_close=bar.close,
                ))
            prior_fast, prior_slow = fast, slow
    return tuple(sorted(signals, key=lambda item: (
        item.legal_execution_at,
        item.symbol,
        item.kind.value,
    )))


def resolve_candidate(
    signal: _Signal, candidates: Iterable[OptionCandidate]
) -> OptionCandidate | None:
    """Apply the frozen front-weekly and candidate-selection predicates."""

    observed = tuple(
        item
        for item in candidates
        if item.underlying_symbol == signal.symbol
        and item.observed_at == signal.legal_execution_at
        and item.right == "CALL"
    )
    session_date = signal.legal_execution_at.astimezone(EASTERN).date()
    expirations = sorted({
        item.expiration
        for item in observed
        if 0 <= (item.expiration - session_date).days <= 5
    })
    if not expirations:
        return None
    target_expiration = expirations[0]
    eligible = [
        item
        for item in observed
        if item.expiration == target_expiration
        and item.strike > signal.spot_close
        and item.ask <= Decimal("0.50")
        and item.bid > Decimal("0.00")
        and item.ask - item.bid <= Decimal("0.03")
        and (
            (item.volume is not None and item.volume >= 10)
            or (item.open_interest is not None and item.open_interest >= 50)
        )
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda item: (
        abs(item.ask - ENTRY_TARGET),
        item.ask - item.bid,
        -(item.open_interest if item.open_interest is not None else -1),
        item.instrument_id,
    ))


def position_quantity(
    allocation: Decimal,
    available_cash: Decimal,
    ask: Decimal,
    mode: ExecutionMode,
) -> int:
    """Apply the frozen whole-contract sizing rule to the current cash balance."""

    entry_price = ask + (ONE_TICK if mode is ExecutionMode.STRESS_1_TICK else Decimal("0"))
    budget = min(allocation, available_cash)
    maximum = int((budget / (entry_price * CONTRACT_MULTIPLIER + Decimal("1"))).to_integral_value(
        rounding=ROUND_DOWN
    ))
    for quantity in range(maximum, 0, -1):
        fee = per_side_fee(quantity)
        cost_per_contract = ask * CONTRACT_MULTIPLIER + fee
        formula_quantity = min(
            int((allocation / cost_per_contract).to_integral_value(rounding=ROUND_DOWN)),
            int((available_cash / cost_per_contract).to_integral_value(rounding=ROUND_DOWN)),
        )
        actual_cost = entry_price * CONTRACT_MULTIPLIER * quantity + fee
        if (
            formula_quantity >= quantity
            and actual_cost <= allocation
            and actual_cost <= available_cash
        ):
            return quantity
    return 0


def _trade_id(
    stream: str,
    mode: ExecutionMode,
    tier: Decimal | None,
    signal: _Signal,
    instrument_id: str,
) -> str:
    identity = "|".join((
        stream,
        mode.value,
        str(tier),
        signal.symbol,
        signal.signal_at.isoformat(),
        instrument_id,
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _metrics(
    signals: list[SignalLedgerEntry],
    trades: list[TradeLedgerEntry],
    initial_equity: Decimal | None,
    terminal_equity: Decimal | None,
) -> SummaryMetrics:
    net_values = [item.net_pnl for item in trades]
    expectancy = (
        _money(sum(net_values, Decimal("0")) / Decimal(len(net_values)))
        if net_values
        else Decimal("0.00")
    )
    profits = sum((value for value in net_values if value > 0), Decimal("0"))
    losses = -sum((value for value in net_values if value < 0), Decimal("0"))
    profit_factor = None if losses == 0 else (profits / losses).quantize(Decimal("0.0001"))
    win_rate = _percentage(sum(value > 0 for value in net_values), len(net_values))
    reference = initial_equity or Decimal("1000.00")
    running = reference
    peak = reference
    maximum_drawdown = Decimal("0")
    for value in net_values:
        running += value
        peak = max(peak, running)
        denominator = Decimal("1000.00") if initial_equity is None else peak
        maximum_drawdown = max(maximum_drawdown, (peak - running) / denominator * Decimal("100"))
    return SummaryMetrics(
        executed_trade_count=len(trades),
        net_expectancy=expectancy,
        profit_factor=profit_factor,
        win_rate=win_rate,
        maximum_drawdown=maximum_drawdown.quantize(PERCENT),
        candidate_skips=sum(
            item.disposition is SignalDisposition.CANDIDATE_UNAVAILABLE_SKIP for item in signals
        ),
        affordability_skips=sum(
            item.disposition is SignalDisposition.SKIPPED_INSUFFICIENT_FUNDS for item in signals
        ),
        active_position_suppressions=sum(
            item.disposition is SignalDisposition.ACTIVE_POSITION_SUPPRESSION for item in signals
        ),
        terminal_equity=_money(terminal_equity) if terminal_equity is not None else None,
    )


def _run_mode(
    simulation: ResearchSimulationInput,
    signals: tuple[_Signal, ...],
    *,
    stream: str,
    mode: ExecutionMode,
    starting_capital: Decimal | None,
) -> ModeResult:
    candidates_by_batch: dict[tuple[str, datetime], list[OptionCandidate]] = defaultdict(list)
    for item in simulation.option_candidates:
        candidates_by_batch[(item.underlying_symbol, item.observed_at)].append(item)
    intervals_by_time: dict[datetime, list[OptionInterval]] = defaultdict(list)
    for item in simulation.option_intervals:
        intervals_by_time[item.completed_at].append(item)
    entries_by_time: dict[datetime, list[_Signal]] = defaultdict(list)
    reversals: dict[str, list[datetime]] = defaultdict(list)
    for signal in signals:
        if signal.kind is SignalKind.BULLISH:
            entries_by_time[signal.legal_execution_at].append(signal)
        else:
            reversals[signal.symbol].append(signal.legal_execution_at)

    account = _Account(
        initial_equity=starting_capital,
        cash=starting_capital,
        latest_bid={},
    )
    active: dict[str, _Position] = {}
    signal_ledger: list[SignalLedgerEntry] = []
    trade_ledger: list[TradeLedgerEntry] = []
    timeline = sorted(set(entries_by_time) | set(intervals_by_time))
    for timestamp in timeline:
        for interval in sorted(
            intervals_by_time.get(timestamp, ()), key=lambda item: item.instrument_id
        ):
            account.latest_bid[interval.instrument_id] = interval.bid_close
            position_entry = next(
                (
                    (symbol, position)
                    for symbol, position in active.items()
                    if position.candidate.instrument_id == interval.instrument_id
                ),
                None,
            )
            if position_entry is None:
                continue
            symbol, position = position_entry
            if interval.completed_at <= position.signal.legal_execution_at:
                continue
            reason: ExitReason | None = None
            if interval.bid_close <= Decimal("0.80") * position.candidate.ask:
                reason = ExitReason.STOP_LOSS
            elif interval.bid_close >= Decimal("1.20") * position.candidate.ask:
                reason = ExitReason.PROFIT_TARGET
            elif interval.interval_start_at in reversals.get(symbol, ()):
                reason = ExitReason.SIGNAL_REVERSAL
            elif interval.interval_start_at.astimezone(EASTERN).time() == time(15, 58):
                reason = ExitReason.SESSION_FORCE_CLOSE
            if reason is None:
                continue
            exit_price = interval.bid_close - (
                ONE_TICK if mode is ExecutionMode.STRESS_1_TICK else Decimal("0")
            )
            exit_price = max(Decimal("0"), exit_price)
            exit_fee = per_side_fee(position.quantity)
            gross = (exit_price - position.entry_price) * CONTRACT_MULTIPLIER * position.quantity
            fees = position.entry_fee + exit_fee
            net = gross - fees
            if account.cash is not None:
                account.cash += exit_price * CONTRACT_MULTIPLIER * position.quantity - exit_fee
            trade_ledger.append(TradeLedgerEntry(
                trade_id=_trade_id(
                    stream,
                    mode,
                    starting_capital,
                    position.signal,
                    position.candidate.instrument_id,
                ),
                stream=stream,
                execution_mode=mode,
                capital_tier=starting_capital,
                underlying=symbol,
                signal_timestamp=position.signal.signal_at,
                legal_entry_timestamp=position.signal.legal_execution_at,
                selected_option_identity=position.candidate.instrument_id,
                expiration=position.candidate.expiration,
                strike=position.candidate.strike,
                right=position.candidate.right,
                selection_bid=position.candidate.bid,
                selection_ask=position.candidate.ask,
                entry_price=_money(position.entry_price),
                exit_timestamp=interval.completed_at,
                exit_price=_money(exit_price),
                exit_reason=reason,
                quantity=position.quantity,
                gross_pnl=_money(gross),
                fees=_money(fees),
                net_pnl=_money(net),
            ))
            del active[symbol]

        batch = sorted(entries_by_time.get(timestamp, ()), key=lambda item: item.symbol)
        if batch:
            if account.cash is None:
                frozen_allocation = None
            else:
                marked_positions = sum((
                        account.latest_bid[position.candidate.instrument_id]
                    * CONTRACT_MULTIPLIER
                    * position.quantity
                    for position in active.values()
                ), Decimal("0"))
                frozen_equity = account.cash + marked_positions
                frozen_allocation = Decimal("0.25") * frozen_equity
            for signal in batch:
                ledger_kwargs = {
                    "stream": stream,
                    "execution_mode": mode,
                    "capital_tier": starting_capital,
                    "symbol": signal.symbol,
                    "signal_at": signal.signal_at,
                    "legal_entry_at": signal.legal_execution_at,
                }
                if signal.symbol in active:
                    signal_ledger.append(SignalLedgerEntry(
                        **ledger_kwargs,
                        disposition=SignalDisposition.ACTIVE_POSITION_SUPPRESSION,
                    ))
                    continue
                candidate = resolve_candidate(
                    signal,
                    candidates_by_batch[(signal.symbol, signal.legal_execution_at)],
                )
                if candidate is None:
                    signal_ledger.append(SignalLedgerEntry(
                        **ledger_kwargs,
                        disposition=SignalDisposition.CANDIDATE_UNAVAILABLE_SKIP,
                    ))
                    continue
                quantity = 1
                if account.cash is not None:
                    assert frozen_allocation is not None
                    quantity = position_quantity(
                        frozen_allocation,
                        account.cash,
                        candidate.ask,
                        mode,
                    )
                if quantity == 0:
                    signal_ledger.append(SignalLedgerEntry(
                        **ledger_kwargs,
                        disposition=SignalDisposition.SKIPPED_INSUFFICIENT_FUNDS,
                        selected_instrument_id=candidate.instrument_id,
                    ))
                    continue
                entry_price = candidate.ask + (
                    ONE_TICK if mode is ExecutionMode.STRESS_1_TICK else Decimal("0")
                )
                entry_fee = per_side_fee(quantity)
                if account.cash is not None:
                    entry_cost = entry_price * CONTRACT_MULTIPLIER * quantity + entry_fee
                    if entry_cost > account.cash:
                        raise ValueError("capital sizing would produce negative cash")
                    account.cash -= entry_cost
                active[signal.symbol] = _Position(
                    signal=signal,
                    candidate=candidate,
                    quantity=quantity,
                    entry_price=entry_price,
                    entry_fee=entry_fee,
                )
                account.latest_bid[candidate.instrument_id] = candidate.bid
                signal_ledger.append(SignalLedgerEntry(
                    **ledger_kwargs,
                    disposition=SignalDisposition.EXECUTED,
                    selected_instrument_id=candidate.instrument_id,
                    quantity=quantity,
                ))
    if active:
        raise ValueError("synthetic input ended with an open overnight position")
    terminal = account.cash if account.cash is not None else None
    return ModeResult(
        execution_mode=mode,
        signal_ledger=tuple(signal_ledger),
        trade_ledger=tuple(trade_ledger),
        summary=_metrics(signal_ledger, trade_ledger, starting_capital, terminal),
    )


def _dual_mode(
    simulation: ResearchSimulationInput,
    signals: tuple[_Signal, ...],
    *,
    stream: str,
    starting_capital: Decimal | None,
) -> DualModeResult:
    baseline = _run_mode(
        simulation,
        signals,
        stream=stream,
        mode=ExecutionMode.BASELINE,
        starting_capital=starting_capital,
    )
    stressed = _run_mode(
        simulation,
        signals,
        stream=stream,
        mode=ExecutionMode.STRESS_1_TICK,
        starting_capital=starting_capital,
    )
    return DualModeResult(
        baseline=baseline,
        stressed=stressed,
        baseline_expectancy=baseline.summary.net_expectancy,
        stressed_expectancy=stressed.summary.net_expectancy,
    )


def simulate_strategy_001(
    simulation: ResearchSimulationInput,
    *,
    policy_path: Path = POLICY_PATH,
) -> Strategy001ResearchReceipt:
    """Run both frozen analytical streams against caller-supplied synthetic evidence."""

    binding = load_policy_binding(
        stage_4_manifest_sha256=simulation.stage_4_manifest_sha256,
        simulator_git_commit_sha=simulation.simulator_git_commit_sha,
        policy_path=policy_path,
    )
    signals = derive_signals(simulation.underlying_bars)
    economics = _dual_mode(
        simulation,
        signals,
        stream="STRATEGY_ECONOMICS",
        starting_capital=None,
    )
    tiers = tuple(
        CapitalTierResult(
            starting_capital=tier,
            allocation_rate=Decimal("0.25"),
            results=_dual_mode(
                simulation,
                signals,
                stream="CAPITAL_FEASIBILITY",
                starting_capital=tier,
            ),
        )
        for tier in CAPITAL_TIERS
    )
    return Strategy001ResearchReceipt(
        schema_version="KAIRO-STRATEGY-001-RESEARCH-RECEIPT-v1.1",
        binding=binding,
        inference_boundary=INFERENCE_BOUNDARY,
        strategy_economics=economics,
        capital_feasibility=tiers,
    )
