"""Deterministic, policy-neutral evidence adapter for Q1 2024 sealed market data.

This module performs representation translation only:
    CanonicalMarketBar dict            -> UnderlyingBar
    CanonicalOptionContractQuote dict  -> OptionCandidate
    CanonicalOptionContractQuote dict  -> OptionInterval

This adapter contains strictly zero strategy filtering, zero contract ranking,
zero signal evaluation, zero fee modeling, zero capital sizing, and zero
simulation execution logic. All strategy and analytical decisions are deferred
entirely to the verified simulator.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Sequence

from engine.research.strategy_001_simulator import (
    OptionCandidate,
    OptionInterval,
    ResearchSimulationInput,
    UnderlyingBar,
)


SEALED_EVIDENCE_IDENTITIES = {
    "TQQQ_BARS": {
        "object_name": "finalization/q1_2024/normalization/c18c20259c70fe62679c834cc1e7db5f8dcd1ccaa45e3c35ad90c7b0481bbf41/TQQQ-bars.json",
        "sha256": "ec847019da3b1c2666e913b5702bedc087087e00622228cc79cc097a7f512404",
        "byte_count": 6022588,
        "bar_count": 23790,
    },
    "SQQQ_BARS": {
        "object_name": "finalization/q1_2024/normalization/c18c20259c70fe62679c834cc1e7db5f8dcd1ccaa45e3c35ad90c7b0481bbf41/SQQQ-bars.json",
        "sha256": "b09e47225a0aec28164116056508c48bbae234df27ee4d322f07cd5fefde6003",
        "byte_count": 5950834,
        "bar_count": 23790,
    },
    "TQQQ_OPTIONS": {
        "object_name": "finalization/q1_2024/normalization/c18c20259c70fe62679c834cc1e7db5f8dcd1ccaa45e3c35ad90c7b0481bbf41/TQQQ-options.json",
        "sha256": "b9836693dba80926f68ee05bcd91352358e46476b04baf1e824556321d9f3750",
        "byte_count": 333973034,
        "decision_intervals": 4516,
    },
    "SQQQ_OPTIONS": {
        "object_name": "finalization/q1_2024/normalization/c18c20259c70fe62679c834cc1e7db5f8dcd1ccaa45e3c35ad90c7b0481bbf41/SQQQ-options.json",
        "sha256": "a7bd64a1246c439c060cc2f48a93a1198b09cf99ec366b4dcc1a073ec0a1b68e",
        "byte_count": 365219051,
        "decision_intervals": 4938,
    },
    "STAGE_4_MANIFEST": {
        "object_name": "manifests/theta_q1_2024_manifest.json",
        "sha256": "24cb3a9e1806b804cda9ca7f2a4e3ef1a0123c8912bf42933a0053b3d43e915a",
        "byte_count": 3594,
    },
}

SIMULATOR_COMMIT_SHA = "18c69771f67d30bf794e08178e3a4f8537502081"
STAGE_4_MANIFEST_SHA256 = (
    "24cb3a9e1806b804cda9ca7f2a4e3ef1a0123c8912bf42933a0053b3d43e915a"
)


def adapt_underlying_bar(raw: dict[str, Any]) -> UnderlyingBar:
    """Translate one CanonicalMarketBar payload into an UnderlyingBar."""
    return UnderlyingBar(
        symbol=str(raw["symbol"]),
        interval_start_at=datetime.fromisoformat(raw["interval_start_at"]),
        completed_at=datetime.fromisoformat(raw["completed_at"]),
        close=Decimal(str(raw["close"])),
    )


def adapt_option_candidate(
    contract: dict[str, Any],
    *,
    observed_at: datetime,
    underlying_symbol: str,
) -> OptionCandidate | None:
    """Translate one CanonicalOptionContractQuote into an OptionCandidate.

    Returns None if:
      - the contract is not a CALL option;
      - the ask price is non-positive or less than the bid price (invalid quote).
    Does NOT perform any strategy filtering (no strike-distance, spread, premium,
    or volume filtering). All qualifying checks are executed by the simulator.
    """
    if contract.get("option_right") != "CALL":
        return None

    ask_price = Decimal(str(contract["ask_price"]))
    bid_price = Decimal(str(contract["bid_price"]))
    if ask_price <= Decimal("0") or ask_price < bid_price:
        return None

    instrument_id = str(
        contract.get("canonical_contract_symbol") or contract["contract_instrument_id"]
    )
    expiration = (
        date.fromisoformat(contract["expiration_date"])
        if isinstance(contract["expiration_date"], str)
        else contract["expiration_date"]
    )

    return OptionCandidate(
        instrument_id=instrument_id,
        underlying_symbol=underlying_symbol,
        observed_at=observed_at,
        expiration=expiration,
        strike=Decimal(str(contract["strike_price"])),
        right="CALL",
        bid=bid_price,
        ask=ask_price,
        volume=int(contract.get("volume") or 0),
        open_interest=int(contract.get("open_interest") or 0),
        is_weekly=True,
    )


def adapt_option_interval(
    contract: dict[str, Any],
    *,
    completed_at: datetime,
) -> OptionInterval:
    """Translate one CanonicalOptionContractQuote into an OptionInterval."""
    instrument_id = str(
        contract.get("canonical_contract_symbol") or contract["contract_instrument_id"]
    )
    bid_price = Decimal(str(contract["bid_price"]))
    return OptionInterval(
        instrument_id=instrument_id,
        interval_start_at=completed_at - timedelta(minutes=1),
        completed_at=completed_at,
        bid_close=max(Decimal("0.00"), bid_price),
    )


def adapt_option_snapshot(
    snapshot: dict[str, Any],
) -> tuple[tuple[OptionCandidate, ...], tuple[OptionInterval, ...]]:
    """Translate all quotes within a CanonicalOptionChainSnapshot into candidates and intervals."""
    observed_at = datetime.fromisoformat(snapshot["canonical_completed_at"])
    symbol = str(snapshot["underlying_symbol"])
    candidates: list[OptionCandidate] = []
    intervals: list[OptionInterval] = []

    for contract in snapshot.get("contracts", ()):
        candidate = adapt_option_candidate(
            contract, observed_at=observed_at, underlying_symbol=symbol
        )
        if candidate is not None:
            candidates.append(candidate)
        intervals.append(adapt_option_interval(contract, completed_at=observed_at))

    return tuple(candidates), tuple(intervals)


def build_simulation_input(
    *,
    raw_bars: Iterable[dict[str, Any]],
    raw_snapshots: Iterable[dict[str, Any]],
    stage_4_manifest_sha256: str = STAGE_4_MANIFEST_SHA256,
    simulator_git_commit_sha: str = SIMULATOR_COMMIT_SHA,
) -> ResearchSimulationInput:
    """Deterministically assemble and validate a ResearchSimulationInput model.

    Ensures:
      - Underlying bars are strictly unique and ordered by (symbol, interval_start_at).
      - Option candidates are strictly unique and ordered by (underlying_symbol, observed_at, instrument_id).
      - Option intervals are strictly unique and ordered by (instrument_id, interval_start_at).
      - Stage 4 manifest SHA-256 and Simulator commit SHA are bound.
    """
    bars = [adapt_underlying_bar(raw) for raw in raw_bars]
    bars.sort(key=lambda b: (b.symbol, b.interval_start_at))

    candidates: list[OptionCandidate] = []
    intervals: list[OptionInterval] = []

    for snapshot in raw_snapshots:
        snap_candidates, snap_intervals = adapt_option_snapshot(snapshot)
        candidates.extend(snap_candidates)
        intervals.extend(snap_intervals)

    candidates.sort(key=lambda c: (c.underlying_symbol, c.observed_at, c.instrument_id))
    intervals.sort(key=lambda i: (i.instrument_id, i.interval_start_at))

    return ResearchSimulationInput(
        underlying_bars=tuple(bars),
        option_candidates=tuple(candidates),
        option_intervals=tuple(intervals),
        stage_4_manifest_sha256=stage_4_manifest_sha256,
        simulator_git_commit_sha=simulator_git_commit_sha,
    )
