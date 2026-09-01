import json
from decimal import Decimal
from enum import StrEnum
from typing import Any


class ResearchMethod(StrEnum):
    TRADE_REMOVAL_COUNTERFACTUAL = "TRADE_REMOVAL_COUNTERFACTUAL"


RESEARCH_SEMANTICS = {
    "authority_mode": "OFFLINE_RESEARCH_ONLY",
    "descriptive_only": True,
    "sample_sufficiency": "NOT_ASSESSED",
    "claims_stateful_replay_equivalence": False,
    "claims_statistical_significance": False,
    "subsequent_state_changes_modeled": False,
}


def compute_max_drawdown(equity_curve: list[Decimal]) -> Decimal:
    peak = Decimal("0.00")
    maximum = Decimal("0.00")
    for equity in equity_curve:
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def serialize_research_manifest(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
