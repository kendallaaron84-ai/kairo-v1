from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from engine.research.q1_evidence_adapter import (
    SEALED_EVIDENCE_IDENTITIES,
    SIMULATOR_COMMIT_SHA,
    STAGE_4_MANIFEST_SHA256,
    adapt_option_candidate,
    adapt_option_interval,
    adapt_option_snapshot,
    adapt_underlying_bar,
    build_simulation_input,
)
from engine.research.strategy_001_simulator import (
    OptionCandidate,
    OptionInterval,
    ResearchSimulationInput,
    UnderlyingBar,
)

ET = ZoneInfo("America/New_York")


def test_adapt_underlying_bar_preserves_representation_and_precision() -> None:
    raw_bar = {
        "symbol": "TQQQ",
        "interval_start_at": "2024-01-02T14:30:00Z",
        "completed_at": "2024-01-02T14:31:00Z",
        "open": "49.35",
        "high": "49.40",
        "low": "49.155",
        "close": "49.395",
        "volume": "2208504",
        "instrument_id": "2a9b0414-a678-5368-ab4c-d37d8720aba6",
    }
    bar = adapt_underlying_bar(raw_bar)

    assert isinstance(bar, UnderlyingBar)
    assert bar.symbol == "TQQQ"
    assert bar.interval_start_at == datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    assert bar.completed_at == datetime(2024, 1, 2, 14, 31, tzinfo=timezone.utc)
    assert bar.close == Decimal("49.395")


def test_adapt_option_candidate_extracts_calls_without_strategy_filtering() -> None:
    observed_at = datetime(2024, 1, 2, 14, 59, tzinfo=timezone.utc)

    # Valid call contract with wide spread and high premium (must NOT be filtered by adapter)
    raw_call = {
        "canonical_contract_symbol": "TQQQ240105C00044500",
        "contract_instrument_id": "fb452276-a4e0-52df-852d-c7bd91528afc",
        "underlying_symbol": "TQQQ",
        "expiration_date": "2024-01-05",
        "option_right": "CALL",
        "strike_price": "44.5",
        "bid_price": "3.90",
        "ask_price": "4.05",
        "volume": 8,
        "open_interest": 343,
        "listing_type": "STANDARD",
    }
    candidate = adapt_option_candidate(raw_call, observed_at=observed_at, underlying_symbol="TQQQ")

    assert isinstance(candidate, OptionCandidate)
    assert candidate.instrument_id == "TQQQ240105C00044500"
    assert candidate.underlying_symbol == "TQQQ"
    assert candidate.observed_at == observed_at
    assert candidate.expiration == date(2024, 1, 5)
    assert candidate.strike == Decimal("44.5")
    assert candidate.right == "CALL"
    assert candidate.bid == Decimal("3.90")
    assert candidate.ask == Decimal("4.05")
    assert candidate.volume == 8
    assert candidate.open_interest == 343
    assert candidate.is_weekly is True


def test_adapt_option_candidate_disqualifies_puts_and_unphysical_quotes() -> None:
    observed_at = datetime(2024, 1, 2, 14, 59, tzinfo=timezone.utc)

    raw_put = {
        "canonical_contract_symbol": "TQQQ240105P00044500",
        "contract_instrument_id": "baadb3e3-85b2-53a4-8fff-c61775e7dc6d",
        "expiration_date": "2024-01-05",
        "option_right": "PUT",
        "strike_price": "44.5",
        "bid_price": "0.09",
        "ask_price": "0.10",
    }
    assert adapt_option_candidate(raw_put, observed_at=observed_at, underlying_symbol="TQQQ") is None

    # Zero ask
    raw_zero_ask = {
        "canonical_contract_symbol": "TQQQ240105C00099000",
        "contract_instrument_id": "11111111-1111-1111-1111-111111111111",
        "expiration_date": "2024-01-05",
        "option_right": "CALL",
        "strike_price": "99.0",
        "bid_price": "0.00",
        "ask_price": "0.00",
    }
    assert adapt_option_candidate(raw_zero_ask, observed_at=observed_at, underlying_symbol="TQQQ") is None

    # Inverted quote (ask < bid)
    raw_inverted = {
        "canonical_contract_symbol": "TQQQ240105C00099000",
        "contract_instrument_id": "11111111-1111-1111-1111-111111111111",
        "expiration_date": "2024-01-05",
        "option_right": "CALL",
        "strike_price": "99.0",
        "bid_price": "0.50",
        "ask_price": "0.40",
    }
    assert adapt_option_candidate(raw_inverted, observed_at=observed_at, underlying_symbol="TQQQ") is None


def test_adapt_option_interval_maps_exact_completed_bid() -> None:
    completed_at = datetime(2024, 1, 2, 14, 59, tzinfo=timezone.utc)
    raw_contract = {
        "canonical_contract_symbol": "TQQQ240105C00044500",
        "contract_instrument_id": "fb452276-a4e0-52df-852d-c7bd91528afc",
        "bid_price": "3.90",
    }
    interval = adapt_option_interval(raw_contract, completed_at=completed_at)

    assert isinstance(interval, OptionInterval)
    assert interval.instrument_id == "TQQQ240105C00044500"
    assert interval.interval_start_at == datetime(2024, 1, 2, 14, 58, tzinfo=timezone.utc)
    assert interval.completed_at == completed_at
    assert interval.bid_close == Decimal("3.90")


def test_adapt_option_snapshot_processes_both_calls_and_intervals() -> None:
    raw_snapshot = {
        "canonical_completed_at": "2024-01-02T14:59:00Z",
        "underlying_symbol": "TQQQ",
        "contracts": [
            {
                "canonical_contract_symbol": "TQQQ240105C00044500",
                "contract_instrument_id": "fb452276-a4e0-52df-852d-c7bd91528afc",
                "expiration_date": "2024-01-05",
                "option_right": "CALL",
                "strike_price": "44.5",
                "bid_price": "3.90",
                "ask_price": "4.05",
            },
            {
                "canonical_contract_symbol": "TQQQ240105P00044500",
                "contract_instrument_id": "baadb3e3-85b2-53a4-8fff-c61775e7dc6d",
                "expiration_date": "2024-01-05",
                "option_right": "PUT",
                "strike_price": "44.5",
                "bid_price": "0.09",
                "ask_price": "0.10",
            },
        ],
    }
    candidates, intervals = adapt_option_snapshot(raw_snapshot)

    # 1 candidate (call only)
    assert len(candidates) == 1
    assert candidates[0].instrument_id == "TQQQ240105C00044500"

    # 2 intervals (both contracts get tracking intervals)
    assert len(intervals) == 2
    assert {i.instrument_id for i in intervals} == {
        "TQQQ240105C00044500",
        "TQQQ240105P00044500",
    }


def test_build_simulation_input_enforces_deterministic_ordering_and_bindings() -> None:
    raw_bars = [
        {
            "symbol": "TQQQ",
            "interval_start_at": "2024-01-02T14:31:00Z",
            "completed_at": "2024-01-02T14:32:00Z",
            "close": "49.39",
        },
        {
            "symbol": "SQQQ",
            "interval_start_at": "2024-01-02T14:30:00Z",
            "completed_at": "2024-01-02T14:31:00Z",
            "close": "12.50",
        },
        {
            "symbol": "TQQQ",
            "interval_start_at": "2024-01-02T14:30:00Z",
            "completed_at": "2024-01-02T14:31:00Z",
            "close": "49.35",
        },
    ]
    raw_snapshots = [
        {
            "canonical_completed_at": "2024-01-02T14:32:00Z",
            "underlying_symbol": "TQQQ",
            "contracts": [
                {
                    "canonical_contract_symbol": "TQQQ-B",
                    "contract_instrument_id": "22222222-2222-2222-2222-222222222222",
                    "expiration_date": "2024-01-05",
                    "option_right": "CALL",
                    "strike_price": "50.0",
                    "bid_price": "0.30",
                    "ask_price": "0.33",
                },
                {
                    "canonical_contract_symbol": "TQQQ-A",
                    "contract_instrument_id": "11111111-1111-1111-1111-111111111111",
                    "expiration_date": "2024-01-05",
                    "option_right": "CALL",
                    "strike_price": "50.0",
                    "bid_price": "0.30",
                    "ask_price": "0.33",
                },
            ],
        }
    ]

    sim_input = build_simulation_input(
        raw_bars=raw_bars,
        raw_snapshots=raw_snapshots,
    )

    assert isinstance(sim_input, ResearchSimulationInput)
    # Bars sorted by (symbol, interval_start_at): SQQQ 14:30, TQQQ 14:30, TQQQ 14:31
    assert [b.symbol for b in sim_input.underlying_bars] == ["SQQQ", "TQQQ", "TQQQ"]
    assert sim_input.underlying_bars[1].interval_start_at < sim_input.underlying_bars[2].interval_start_at

    # Candidates sorted by (underlying, observed_at, instrument_id): TQQQ-A before TQQQ-B
    assert [c.instrument_id for c in sim_input.option_candidates] == ["TQQQ-A", "TQQQ-B"]

    # Governance bindings
    assert sim_input.stage_4_manifest_sha256 == STAGE_4_MANIFEST_SHA256
    assert sim_input.simulator_git_commit_sha == SIMULATOR_COMMIT_SHA
