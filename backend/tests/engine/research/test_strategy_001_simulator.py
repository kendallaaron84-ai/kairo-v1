from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from engine.research.strategy_001_simulator import (
    CAPITAL_TIERS,
    CANDIDATE_DIAGNOSTIC,
    DATASET_ID,
    INFERENCE_BOUNDARY,
    POLICY_GIT_COMMIT,
    POLICY_ID,
    POLICY_SHA256,
    ExecutionMode,
    ExitReason,
    OptionCandidate,
    OptionInterval,
    ResearchSimulationInput,
    SignalDisposition,
    SignalKind,
    UnderlyingBar,
    derive_signals,
    load_policy_binding,
    per_side_fee,
    position_quantity,
    resolve_candidate,
    simulate_strategy_001,
)


ET = ZoneInfo("America/New_York")
ZERO_HASH = "0" * 64
ONE_HASH = "1" * 40


def _at(hour: int, minute: int) -> datetime:
    return datetime(2024, 1, 2, hour, minute, tzinfo=ET)


def _bars(symbol: str = "TQQQ", closes: tuple[str, ...] = ("100", "110", "90", "90")):
    return tuple(
        UnderlyingBar(
            symbol=symbol,
            interval_start_at=_at(9, 30) + timedelta(minutes=index),
            completed_at=_at(9, 31) + timedelta(minutes=index),
            close=close,
        )
        for index, close in enumerate(closes)
    )


def _candidate(
    *,
    symbol: str = "TQQQ",
    observed_at: datetime | None = None,
    instrument_id: str = "TQQQ-20240102-C-111",
    expiration: date = date(2024, 1, 2),
    strike: str = "111",
    bid: str = "0.38",
    ask: str = "0.40",
    volume: int | None = 10,
    open_interest: int | None = 50,
) -> OptionCandidate:
    return OptionCandidate(
        instrument_id=instrument_id,
        underlying_symbol=symbol,
        observed_at=observed_at or _at(9, 32),
        expiration=expiration,
        strike=strike,
        right="CALL",
        bid=bid,
        ask=ask,
        volume=volume,
        open_interest=open_interest,
    )


def _interval(
    start: datetime,
    bid_close: str,
    instrument_id: str = "TQQQ-20240102-C-111",
) -> OptionInterval:
    return OptionInterval(
        instrument_id=instrument_id,
        interval_start_at=start,
        completed_at=start + timedelta(minutes=1),
        bid_close=bid_close,
    )


def _simulation(
    *,
    bars: tuple[UnderlyingBar, ...] | None = None,
    candidates: tuple[OptionCandidate, ...] | None = None,
    intervals: tuple[OptionInterval, ...] | None = None,
) -> ResearchSimulationInput:
    return ResearchSimulationInput(
        underlying_bars=bars if bars is not None else _bars(),
        option_candidates=candidates if candidates is not None else (_candidate(),),
        option_intervals=intervals
        if intervals is not None
        else (
            _interval(_at(9, 32), "0.40"),
            _interval(_at(9, 33), "0.42"),
        ),
        stage_4_manifest_sha256=ZERO_HASH,
        simulator_git_commit_sha=ONE_HASH,
    )


def test_ema_cross_uses_completed_bar_and_enters_only_at_t_plus_one() -> None:
    signals = derive_signals(_bars())

    assert [(item.kind, item.signal_at, item.legal_execution_at) for item in signals] == [
        (SignalKind.BULLISH, _at(9, 32), _at(9, 32)),
        (SignalKind.BEARISH, _at(9, 33), _at(9, 33)),
    ]
    assert signals[0].signal_at == _bars()[1].completed_at
    assert signals[0].legal_execution_at == _bars()[2].interval_start_at


def test_candidate_filtering_zero_dte_precedence_and_tie_breaking() -> None:
    signal = derive_signals(_bars())[0]
    candidates = (
        _candidate(instrument_id="NEXT-DATE", expiration=date(2024, 1, 3), ask="0.35", bid="0.34"),
        _candidate(instrument_id="DTE-TOO-HIGH", expiration=date(2024, 1, 10), ask="0.35", bid="0.34"),
        _candidate(instrument_id="ASK-TOO-HIGH", ask="0.51", bid="0.49"),
        _candidate(instrument_id="ZERO-BID", ask="0.02", bid="0.00"),
        _candidate(instrument_id="WIDE", ask="0.40", bid="0.36"),
        _candidate(instrument_id="ILLIQUID", volume=9, open_interest=49),
        _candidate(instrument_id="NOT-OTM", strike="110"),
        _candidate(instrument_id="B", ask="0.36", bid="0.34", open_interest=100),
        _candidate(instrument_id="A", ask="0.36", bid="0.34", open_interest=100),
        _candidate(instrument_id="LOWER-OI", ask="0.36", bid="0.34", open_interest=99),
        _candidate(instrument_id="WIDER", ask="0.34", bid="0.31", open_interest=200),
    )

    assert resolve_candidate(signal, candidates).instrument_id == "A"


def test_candidate_unavailable_is_logged_without_a_trade() -> None:
    receipt = simulate_strategy_001(_simulation(candidates=()))
    result = receipt.strategy_economics.baseline

    assert result.trade_ledger == ()
    assert result.signal_ledger[0].disposition is SignalDisposition.CANDIDATE_UNAVAILABLE_SKIP
    assert result.summary.candidate_skips == 1


def test_baseline_and_stress_use_ask_bid_and_distinct_ledgers() -> None:
    receipt = simulate_strategy_001(_simulation())
    baseline = receipt.strategy_economics.baseline.trade_ledger[0]
    stressed = receipt.strategy_economics.stressed.trade_ledger[0]

    assert (baseline.entry_price, baseline.exit_price) == (Decimal("0.40"), Decimal("0.42"))
    assert (baseline.gross_pnl, baseline.fees, baseline.net_pnl) == (
        Decimal("2.00"), Decimal("2.00"), Decimal("0.00")
    )
    assert (stressed.entry_price, stressed.exit_price) == (Decimal("0.41"), Decimal("0.41"))
    assert (stressed.gross_pnl, stressed.fees, stressed.net_pnl) == (
        Decimal("0.00"), Decimal("2.00"), Decimal("-2.00")
    )
    assert baseline.execution_mode is ExecutionMode.BASELINE
    assert stressed.execution_mode is ExecutionMode.STRESS_1_TICK


def test_ticket_fee_floor_and_multi_contract_fee() -> None:
    assert per_side_fee(1) == Decimal("1.00")
    assert per_side_fee(2) == Decimal("1.40")
    assert per_side_fee(10) == Decimal("7.00")
    with pytest.raises(ValueError, match="positive"):
        per_side_fee(0)


def test_sizing_uses_frozen_allocation_and_recomputed_available_cash() -> None:
    frozen_allocation = Decimal("125.00")

    assert position_quantity(
        frozen_allocation, Decimal("500.00"), Decimal("0.40"), ExecutionMode.BASELINE
    ) == 2
    assert position_quantity(
        frozen_allocation, Decimal("50.00"), Decimal("0.40"), ExecutionMode.BASELINE
    ) == 1
    assert position_quantity(
        frozen_allocation, Decimal("40.00"), Decimal("0.40"), ExecutionMode.BASELINE
    ) == 0


@pytest.mark.parametrize(
    ("closing_bids", "expected_reason", "expected_exit"),
    [
        (("0.33", "0.32"), ExitReason.STOP_LOSS, Decimal("0.32")),
        (("0.47", "0.48"), ExitReason.PROFIT_TARGET, Decimal("0.48")),
    ],
)
def test_stop_and_target_use_only_completed_interval_bid_close_and_precede_reversal(
    closing_bids: tuple[str, str],
    expected_reason: ExitReason,
    expected_exit: Decimal,
) -> None:
    receipt = simulate_strategy_001(_simulation(intervals=(
        _interval(_at(9, 32), closing_bids[0]),
        _interval(_at(9, 33), closing_bids[1]),
    )))
    trade = receipt.strategy_economics.baseline.trade_ledger[0]

    assert trade.exit_timestamp == _at(9, 34)
    assert trade.exit_reason is expected_reason
    assert trade.exit_price == expected_exit


def test_reversal_exits_on_its_next_legal_interval() -> None:
    trade = simulate_strategy_001(_simulation()).strategy_economics.baseline.trade_ledger[0]

    assert trade.exit_reason is ExitReason.SIGNAL_REVERSAL
    assert trade.exit_timestamp == _at(9, 34)


def test_1558_interval_force_closes_at_its_completed_bid() -> None:
    bars = tuple(
        UnderlyingBar(
            symbol="TQQQ",
            interval_start_at=_at(15, 55) + timedelta(minutes=index),
            completed_at=_at(15, 56) + timedelta(minutes=index),
            close=close,
        )
        for index, close in enumerate(("100", "110", "111", "112"))
    )
    instrument = "TQQQ-FORCE"
    receipt = simulate_strategy_001(_simulation(
        bars=bars,
        candidates=(_candidate(
            observed_at=_at(15, 57), instrument_id=instrument, strike="111.5"
        ),),
        intervals=(
            _interval(_at(15, 57), "0.40", instrument),
            _interval(_at(15, 58), "0.41", instrument),
        ),
    ))
    trade = receipt.strategy_economics.baseline.trade_ledger[0]

    assert trade.exit_reason is ExitReason.SESSION_FORCE_CLOSE
    assert trade.exit_timestamp == _at(15, 59)
    assert trade.exit_price == Decimal("0.41")


def test_active_position_suppresses_later_entry_for_same_symbol() -> None:
    bars = _bars(closes=("100", "110", "90", "120", "120"))
    first = _candidate()
    second = _candidate(observed_at=_at(9, 34), instrument_id="TQQQ-SECOND", strike="121")
    receipt = simulate_strategy_001(_simulation(
        bars=bars,
        candidates=(first, second),
        intervals=(
            _interval(_at(9, 32), "0.40"),
            _interval(_at(15, 58), "0.41"),
        ),
    ))
    result = receipt.strategy_economics.baseline

    assert len(result.trade_ledger) == 1
    assert result.summary.active_position_suppressions == 1
    assert result.signal_ledger[1].disposition is SignalDisposition.ACTIVE_POSITION_SUPPRESSION


def test_six_tiers_25_percent_downward_sizing_and_affordability_skip() -> None:
    receipt = simulate_strategy_001(_simulation())

    assert tuple(item.starting_capital for item in receipt.capital_feasibility) == CAPITAL_TIERS
    assert all(item.allocation_rate == Decimal("0.25") for item in receipt.capital_feasibility)
    tier_100 = receipt.capital_feasibility[0].results.baseline
    tier_250 = receipt.capital_feasibility[1].results.baseline
    assert tier_100.trade_ledger == ()
    assert tier_100.summary.affordability_skips == 1
    assert tier_100.signal_ledger[0].disposition is SignalDisposition.SKIPPED_INSUFFICIENT_FUNDS
    assert tier_250.trade_ledger[0].quantity == 1
    assert receipt.capital_feasibility[2].results.baseline.trade_ledger[0].quantity == 2
    assert receipt.capital_feasibility[3].results.baseline.trade_ledger[0].quantity == 5
    assert tier_250.summary.terminal_equity == Decimal("250.00")


def test_equal_timestamp_batches_are_sqqq_then_tqqq_with_frozen_allocation() -> None:
    bars = _bars("SQQQ") + _bars("TQQQ")
    sqqq = _candidate(symbol="SQQQ", instrument_id="SQQQ-C", strike="111")
    tqqq = _candidate(symbol="TQQQ", instrument_id="TQQQ-C", strike="111")
    intervals = (
        _interval(_at(9, 32), "0.40", "SQQQ-C"),
        _interval(_at(9, 32), "0.40", "TQQQ-C"),
        _interval(_at(9, 33), "0.42", "SQQQ-C"),
        _interval(_at(9, 33), "0.42", "TQQQ-C"),
    )
    receipt = simulate_strategy_001(_simulation(
        bars=bars, candidates=(tqqq, sqqq), intervals=intervals
    ))
    result = receipt.capital_feasibility[1].results.baseline
    entries = [
        item
        for item in result.signal_ledger
        if item.disposition is SignalDisposition.EXECUTED
    ]

    assert [(item.symbol, item.quantity) for item in entries] == [("SQQQ", 1), ("TQQQ", 1)]
    assert result.summary.terminal_equity == Decimal("250.00")


def test_changing_equity_compounds_later_tier_quantity() -> None:
    bars = _bars(closes=("100", "110", "90", "120", "120", "120"))
    first = _candidate(ask="0.40", bid="0.38")
    second = _candidate(
        observed_at=_at(9, 34), instrument_id="TQQQ-SECOND", strike="121", ask="0.40", bid="0.38"
    )
    receipt = simulate_strategy_001(_simulation(
        bars=bars,
        candidates=(first, second),
        intervals=(
            _interval(_at(9, 32), "0.50"),
            _interval(_at(9, 34), "0.50", "TQQQ-SECOND"),
        ),
    ))
    trades = receipt.capital_feasibility[2].results.baseline.trade_ledger

    assert len(trades) == 2
    assert trades[1].quantity > trades[0].quantity


def test_policy_hash_mismatch_aborts_before_simulation(tmp_path: Path) -> None:
    altered = tmp_path / "research-policy-v1.1.md"
    altered.write_text("not the frozen policy", encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        load_policy_binding(
            stage_4_manifest_sha256=ZERO_HASH,
            simulator_git_commit_sha=ONE_HASH,
            policy_path=altered,
        )


def test_receipt_is_deterministic_and_carries_all_governance_bindings() -> None:
    simulation = _simulation()
    first = simulate_strategy_001(simulation)
    second = simulate_strategy_001(simulation)
    decoded = json.loads(first.canonical_bytes())

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.sha256 == second.sha256
    assert decoded["binding"] == {
        "candidate_diagnostic": CANDIDATE_DIAGNOSTIC,
        "dataset_id": DATASET_ID,
        "live_capital_authorization": False,
        "policy_git_commit": POLICY_GIT_COMMIT,
        "policy_id": POLICY_ID,
        "policy_sha256": POLICY_SHA256,
        "q1_qualification": "FAIL — 41.58%",
        "simulator_git_commit_sha": ONE_HASH,
        "stage_4_manifest_sha256": ZERO_HASH,
    }
    assert decoded["inference_boundary"] == INFERENCE_BOUNDARY
