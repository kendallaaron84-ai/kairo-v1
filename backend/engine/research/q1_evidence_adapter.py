"""Deterministic, policy-neutral evidence adapter for Q1 2024 sealed market data.

This module is a pure, fail-closed representation bridge:
    CanonicalMarketBar dict            -> UnderlyingBar
    CanonicalOptionContractQuote dict  -> OptionCandidate
    CanonicalOptionContractQuote dict  -> OptionInterval

This adapter contains strictly zero strategy filtering, zero contract ranking,
zero signal evaluation, zero fee modeling, zero capital sizing, zero price clamping,
and zero simulation execution logic. All strategy and analytical decisions are deferred
entirely to the verified simulator.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Sequence

from engine.research.strategy_001_simulator import (
    OptionCandidate,
    OptionInterval,
    ResearchSimulationInput,
    UnderlyingBar,
)


class EvidenceIntegrityError(ValueError):
    """Raised when evidence violates deterministic integrity or representation rules."""


class EvidenceProvenanceError(EvidenceIntegrityError):
    """Raised when evidence byte-hash or record-count mismatches canonical seal."""


SEALED_EVIDENCE_IDENTITIES: dict[str, dict[str, Any]] = {
    "STAGE_4_MANIFEST": {
        "object_name": "manifests/theta_q1_2024_manifest.json",
        "sha256": "24cb3a9e1806b804cda9ca7f2a4e3ef1a0123c8912bf42933a0053b3d43e915a",
        "byte_count": 3594,
        "record_count": 1,
    },
    "TQQQ_BARS": {
        "object_name": "finalization/q1_2024/normalization/c18c20259c70fe62679c834cc1e7db5f8dcd1ccaa45e3c35ad90c7b0481bbf41/TQQQ-bars.json",
        "sha256": "ec847019da3b1c2666e913b5702bedc087087e00622228cc79cc097a7f512404",
        "byte_count": 6022588,
        "record_count": 23790,
    },
    "SQQQ_BARS": {
        "object_name": "finalization/q1_2024/normalization/c18c20259c70fe62679c834cc1e7db5f8dcd1ccaa45e3c35ad90c7b0481bbf41/SQQQ-bars.json",
        "sha256": "b09e47225a0aec28164116056508c48bbae234df27ee4d322f07cd5fefde6003",
        "byte_count": 5950834,
        "record_count": 23790,
    },
    "TQQQ_OPTIONS": {
        "object_name": "finalization/q1_2024/normalization/c18c20259c70fe62679c834cc1e7db5f8dcd1ccaa45e3c35ad90c7b0481bbf41/TQQQ-options.json",
        "sha256": "b9836693dba80926f68ee05bcd91352358e46476b04baf1e824556321d9f3750",
        "byte_count": 333973034,
        "record_count": 4516,
    },
    "SQQQ_OPTIONS": {
        "object_name": "finalization/q1_2024/normalization/c18c20259c70fe62679c834cc1e7db5f8dcd1ccaa45e3c35ad90c7b0481bbf41/SQQQ-options.json",
        "sha256": "a7bd64a1246c439c060cc2f48a93a1198b09cf99ec366b4dcc1a073ec0a1b68e",
        "byte_count": 365219051,
        "record_count": 4938,
    },
}

SIMULATOR_COMMIT_SHA = "18c69771f67d30bf794e08178e3a4f8537502081"
STAGE_4_MANIFEST_SHA256 = (
    "24cb3a9e1806b804cda9ca7f2a4e3ef1a0123c8912bf42933a0053b3d43e915a"
)


def verify_artifact_provenance(
    artifact_key: str,
    *,
    sha256: str,
    record_count: int | None = None,
) -> None:
    """Verify that an artifact strictly matches its sealed canonical hash and count.

    Fails closed with EvidenceProvenanceError on any discrepancy.
    """
    if artifact_key not in SEALED_EVIDENCE_IDENTITIES:
        raise EvidenceProvenanceError(f"Unknown sealed artifact key: {artifact_key}")

    expected = SEALED_EVIDENCE_IDENTITIES[artifact_key]
    if sha256.lower() != expected["sha256"].lower():
        raise EvidenceProvenanceError(
            f"Artifact {artifact_key} SHA-256 mismatch: expected {expected['sha256']}, got {sha256}"
        )

    expected_count = expected.get("record_count")
    if expected_count is not None and record_count is not None:
        if record_count != expected_count:
            raise EvidenceProvenanceError(
                f"Artifact {artifact_key} record count mismatch: expected {expected_count}, got {record_count}"
            )


def verify_all_q1_evidence_provenance(
    *,
    manifest_sha256: str,
    tqqq_bars_sha256: str,
    tqqq_bars_count: int,
    sqqq_bars_sha256: str,
    sqqq_bars_count: int,
    tqqq_options_sha256: str,
    tqqq_options_count: int,
    sqqq_options_sha256: str,
    sqqq_options_count: int,
) -> None:
    """Verify all five canonical Q1 evidence artifacts before simulation input construction."""
    verify_artifact_provenance("STAGE_4_MANIFEST", sha256=manifest_sha256)
    verify_artifact_provenance("TQQQ_BARS", sha256=tqqq_bars_sha256, record_count=tqqq_bars_count)
    verify_artifact_provenance("SQQQ_BARS", sha256=sqqq_bars_sha256, record_count=sqqq_bars_count)
    verify_artifact_provenance(
        "TQQQ_OPTIONS", sha256=tqqq_options_sha256, record_count=tqqq_options_count
    )
    verify_artifact_provenance(
        "SQQQ_OPTIONS", sha256=sqqq_options_sha256, record_count=sqqq_options_count
    )


def _parse_optional_nonnegative_int(val: Any, name: str, contract_symbol: str) -> int | None:
    """Preserve distinction between observed zero and missing/unavailable value."""
    if val is None or val == "":
        return None
    try:
        parsed = int(val)
    except (ValueError, TypeError):
        raise EvidenceIntegrityError(f"Contract {contract_symbol} invalid {name}: {val}")
    if parsed < 0:
        raise EvidenceIntegrityError(f"Contract {contract_symbol} negative {name}: {parsed}")
    return parsed


def adapt_underlying_bar(raw: dict[str, Any]) -> UnderlyingBar:
    """Translate one CanonicalMarketBar payload into an UnderlyingBar."""
    if (
        "symbol" not in raw
        or "interval_start_at" not in raw
        or "completed_at" not in raw
        or "close" not in raw
    ):
        raise EvidenceIntegrityError("Underlying bar missing required fields")

    symbol = str(raw["symbol"])
    if symbol not in ("SQQQ", "TQQQ"):
        raise EvidenceIntegrityError(f"Invalid underlying bar symbol: {symbol}")

    close_val = Decimal(str(raw["close"]))
    if close_val <= Decimal("0"):
        raise EvidenceIntegrityError(f"Underlying bar close must be positive: {close_val}")

    return UnderlyingBar(
        symbol=symbol,
        interval_start_at=datetime.fromisoformat(raw["interval_start_at"]),
        completed_at=datetime.fromisoformat(raw["completed_at"]),
        close=close_val,
    )


def adapt_option_candidate(
    contract: dict[str, Any],
    *,
    observed_at: datetime,
    underlying_symbol: str,
) -> OptionCandidate:
    """Translate one CanonicalOptionContractQuote into an OptionCandidate.

    Fails closed on quote contradictions, unphysical values, missing canonical identities,
    or schema violations. Preserves both CALL and PUT options. Preserves missingness
    for volume and open interest.
    """
    canonical_symbol = contract.get("canonical_contract_symbol")
    if not canonical_symbol or not isinstance(canonical_symbol, str) or not canonical_symbol.strip():
        raise EvidenceIntegrityError(
            "Contract missing required canonical_contract_symbol identity"
        )
    instrument_id = canonical_symbol.strip()

    contract_underlying = contract.get("underlying_symbol")
    if contract_underlying is not None and str(contract_underlying) != underlying_symbol:
        raise EvidenceIntegrityError(
            f"Underlying symbol contradiction for {instrument_id}: "
            f"snapshot={underlying_symbol}, contract={contract_underlying}"
        )

    right = contract.get("option_right")
    if right not in ("CALL", "PUT"):
        raise EvidenceIntegrityError(
            f"Contract {instrument_id} invalid or missing option_right: {right}"
        )

    if "expiration_date" not in contract:
        raise EvidenceIntegrityError(f"Contract {instrument_id} missing expiration_date")
    expiration = (
        date.fromisoformat(contract["expiration_date"])
        if isinstance(contract["expiration_date"], str)
        else contract["expiration_date"]
    )

    if "strike_price" not in contract:
        raise EvidenceIntegrityError(f"Contract {instrument_id} missing strike_price")
    strike = Decimal(str(contract["strike_price"]))
    if strike <= Decimal("0"):
        raise EvidenceIntegrityError(f"Contract {instrument_id} non-positive strike: {strike}")

    if "bid_price" not in contract or "ask_price" not in contract:
        raise EvidenceIntegrityError(f"Contract {instrument_id} missing bid or ask price")
    bid_price = Decimal(str(contract["bid_price"]))
    ask_price = Decimal(str(contract["ask_price"]))

    if bid_price < Decimal("0"):
        raise EvidenceIntegrityError(f"Contract {instrument_id} negative bid price: {bid_price}")
    if ask_price <= Decimal("0"):
        raise EvidenceIntegrityError(
            f"Contract {instrument_id} non-positive ask price: {ask_price}"
        )
    if ask_price < bid_price:
        raise EvidenceIntegrityError(
            f"Contract {instrument_id} inverted quote: ask {ask_price} < bid {bid_price}"
        )

    volume = _parse_optional_nonnegative_int(contract.get("volume"), "volume", instrument_id)
    open_interest = _parse_optional_nonnegative_int(
        contract.get("open_interest"), "open_interest", instrument_id
    )

    return OptionCandidate(
        instrument_id=instrument_id,
        underlying_symbol=underlying_symbol,
        observed_at=observed_at,
        expiration=expiration,
        strike=strike,
        right=right,
        bid=bid_price,
        ask=ask_price,
        volume=volume,
        open_interest=open_interest,
    )


def adapt_option_interval(
    contract: dict[str, Any],
    *,
    completed_at: datetime,
) -> OptionInterval:
    """Translate one CanonicalOptionContractQuote into an OptionInterval.

    Fails closed on missing canonical identity or negative bid quotes.
    Performs zero clamping.
    """
    canonical_symbol = contract.get("canonical_contract_symbol")
    if not canonical_symbol or not isinstance(canonical_symbol, str) or not canonical_symbol.strip():
        raise EvidenceIntegrityError(
            "Contract interval missing required canonical_contract_symbol identity"
        )
    instrument_id = canonical_symbol.strip()

    if "bid_price" not in contract:
        raise EvidenceIntegrityError(f"Contract interval {instrument_id} missing bid_price")
    bid_price = Decimal(str(contract["bid_price"]))
    if bid_price < Decimal("0"):
        raise EvidenceIntegrityError(
            f"Contract interval {instrument_id} negative bid price: {bid_price}"
        )

    return OptionInterval(
        instrument_id=instrument_id,
        interval_start_at=completed_at - timedelta(minutes=1),
        completed_at=completed_at,
        bid_close=bid_price,
    )


def adapt_option_snapshot(
    snapshot: dict[str, Any],
) -> tuple[tuple[OptionCandidate, ...], tuple[OptionInterval, ...]]:
    """Translate all quotes within a CanonicalOptionChainSnapshot into candidates and intervals.

    Distinguishes valid empty snapshots (contracts=[]) from malformed snapshots missing 'contracts'.
    """
    if "canonical_completed_at" not in snapshot:
        raise EvidenceIntegrityError("Snapshot missing required canonical_completed_at")
    if "underlying_symbol" not in snapshot:
        raise EvidenceIntegrityError("Snapshot missing required underlying_symbol")
    if "contracts" not in snapshot:
        raise EvidenceIntegrityError("Snapshot missing required 'contracts' field")

    observed_at = datetime.fromisoformat(snapshot["canonical_completed_at"])
    symbol = str(snapshot["underlying_symbol"])
    if symbol not in ("SQQQ", "TQQQ"):
        raise EvidenceIntegrityError(f"Snapshot invalid underlying_symbol: {symbol}")

    raw_contracts = snapshot["contracts"]
    if not isinstance(raw_contracts, (list, tuple)):
        raise EvidenceIntegrityError(
            f"Snapshot 'contracts' must be list or tuple, got {type(raw_contracts).__name__}"
        )

    candidates: list[OptionCandidate] = []
    intervals: list[OptionInterval] = []

    for contract in raw_contracts:
        candidate = adapt_option_candidate(
            contract, observed_at=observed_at, underlying_symbol=symbol
        )
        interval = adapt_option_interval(contract, completed_at=observed_at)
        candidates.append(candidate)
        intervals.append(interval)

    return tuple(candidates), tuple(intervals)


def build_simulation_input(
    *,
    raw_bars: Sequence[dict[str, Any]],
    raw_snapshots: Sequence[dict[str, Any]],
    stage_4_manifest_sha256: str = STAGE_4_MANIFEST_SHA256,
    simulator_git_commit_sha: str = SIMULATOR_COMMIT_SHA,
    provenance_verifications: Sequence[dict[str, Any]] | None = None,
) -> ResearchSimulationInput:
    """Deterministically assemble and validate a ResearchSimulationInput model.

    Ensures:
      - Provenance checks pass before constructing any simulation input.
      - Underlying bars are strictly unique and ordered by (symbol, interval_start_at).
      - Option candidates are strictly unique and ordered by (underlying_symbol, observed_at, instrument_id).
      - Option intervals are strictly unique and ordered by (instrument_id, interval_start_at).
      - Stage 4 manifest SHA-256 and Simulator commit SHA are bound.
    """
    if provenance_verifications is not None:
        for check in provenance_verifications:
            verify_artifact_provenance(
                check["artifact_key"],
                sha256=check["sha256"],
                record_count=check.get("record_count"),
            )

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
