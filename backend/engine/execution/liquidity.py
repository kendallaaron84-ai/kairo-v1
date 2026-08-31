from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from engine.execution.models import ExecutionQuote, LiquidityFidelityTier, PaperEngineConfig


@dataclass(frozen=True)
class LiquidityDecision:
    fill_quantity: Decimal
    policy_version: str
    metadata: dict[str, Any]


def evaluate_liquidity(
    *,
    side: str,
    remaining_quantity: Decimal,
    quote: ExecutionQuote,
    config: PaperEngineConfig,
) -> LiquidityDecision:
    tier = quote.fidelity_tier
    if tier is LiquidityFidelityTier.TIER_1_QUOTE_DEPTH:
        available = quote.ask_size if side == "BUY" else quote.bid_size
        assert available is not None
        return LiquidityDecision(
            fill_quantity=min(remaining_quantity, available),
            policy_version=config.quote_depth_policy_version,
            metadata={
                "quoted_depth_used": True,
                "available_quantity": str(available),
                "queue_position_inferred": False,
            },
        )
    if tier is LiquidityFidelityTier.TIER_2_TRADE_HISTORY:
        assert quote.trade_size is not None
        return LiquidityDecision(
            fill_quantity=min(remaining_quantity, quote.trade_size),
            policy_version=config.trade_history_policy_version,
            metadata={
                "subsequent_print_used": True,
                "print_quantity": str(quote.trade_size),
                "queue_position_inferred": False,
            },
        )
    return LiquidityDecision(
        fill_quantity=remaining_quantity,
        policy_version=config.bar_only_policy_version,
        metadata={
            "coarse_full_fill_hypothesis": True,
            "execution_guaranteed": False,
            "bar_volume_used_as_depth": False,
            "quoted_depth_inferred": False,
            "queue_position_inferred": False,
            "partial_fill_capacity_inferred": False,
            "conservative_reference": "BAR_HIGH_BUY_BAR_LOW_SELL",
        },
    )
