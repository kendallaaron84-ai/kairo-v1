from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.db.models.ledger import MarketSnapshot
from engine.treasury.models import TreasuryExecutionPolicyConfig


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    failed_gate: str | None = None
    observed_value: Decimal | None = None
    threshold_value: Decimal | None = None


def evaluate_execution_gates(
    snapshot: MarketSnapshot,
    *,
    occurred_at: datetime,
    policy: TreasuryExecutionPolicyConfig,
) -> GateResult:
    if occurred_at.tzinfo is None or occurred_at.tzinfo.utcoffset(occurred_at) is None:
        raise ValueError("occurred_at must be timezone-aware")
    if snapshot.captured_at.tzinfo is None or snapshot.captured_at.utcoffset() is None:
        return GateResult(False, "QUOTE_TIMESTAMP")
    age = Decimal(str((occurred_at - snapshot.captured_at).total_seconds()))
    if age < 0 or age > policy.max_quote_age_seconds:
        return GateResult(False, "QUOTE_FRESHNESS", age, policy.max_quote_age_seconds)
    if snapshot.bid is None or snapshot.ask is None:
        return GateResult(False, "BOOK_VALIDITY")
    bid, ask = Decimal(snapshot.bid), Decimal(snapshot.ask)
    if bid <= 0 or ask <= 0 or ask < bid:
        return GateResult(False, "BOOK_VALIDITY")
    spread_bps = ((ask - bid) / ask) * Decimal("10000")
    if spread_bps > policy.max_relative_spread_bps:
        return GateResult(
            False, "SPREAD_CEILING", spread_bps, policy.max_relative_spread_bps
        )
    payload = snapshot.payload or {}
    if payload.get("luld_halted") is not False:
        return GateResult(False, "LULD_OR_EXCHANGE_HALT")
    if payload.get("market_open") is not True or payload.get("regular_session") is not True:
        return GateResult(False, "REGULAR_MARKET_SESSION")
    return GateResult(True)
