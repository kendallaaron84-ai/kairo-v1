from dataclasses import dataclass
from decimal import Decimal

from engine.execution.models import ExecutionQuote, LiquidityFidelityTier


@dataclass(frozen=True)
class PriceModelResult:
    reference_price: Decimal
    effective_price: Decimal | None
    matched: bool


def reference_price(*, side: str, quote: ExecutionQuote) -> Decimal:
    if quote.fidelity_tier is LiquidityFidelityTier.TIER_1_QUOTE_DEPTH:
        value = quote.ask if side == "BUY" else quote.bid
    elif quote.fidelity_tier is LiquidityFidelityTier.TIER_2_TRADE_HISTORY:
        value = quote.trade_price
    else:
        value = quote.bar_high if side == "BUY" else quote.bar_low
    if value is None:
        raise ValueError("required execution reference price is absent")
    return value


def model_execution_price(
    *,
    side: str,
    order_type: str,
    limit_price: Decimal | None,
    quote: ExecutionQuote,
    slippage_rate: Decimal,
) -> PriceModelResult:
    reference = reference_price(side=side, quote=quote)
    if order_type == "LIMIT":
        if limit_price is None:
            raise ValueError("limit order is missing its canonical limit price")
        if (side == "BUY" and reference > limit_price) or (
            side == "SELL" and reference < limit_price
        ):
            return PriceModelResult(reference, None, False)

    direction = Decimal("1") if side == "BUY" else Decimal("-1")
    modeled = reference * (Decimal("1") + direction * slippage_rate)
    if limit_price is not None:
        modeled = min(modeled, limit_price) if side == "BUY" else max(modeled, limit_price)
    return PriceModelResult(reference, modeled, True)


def adverse_slippage_usd(
    *,
    reference_price: Decimal,
    execution_price: Decimal,
    quantity: Decimal,
    contract_multiplier: Decimal,
) -> Decimal:
    return abs(execution_price - reference_price) * quantity * contract_multiplier
