from decimal import Decimal

from app.domain.enums import OrderSide
from engine.risk.models import (
    IntentRiskMetrics,
    RiskClassification,
    RiskEvaluationRequest,
)


def _signed_delta(side: OrderSide, quantity: Decimal) -> Decimal:
    return quantity if side is OrderSide.BUY else -quantity


def classify_intent(
    request: RiskEvaluationRequest,
) -> tuple[RiskClassification, IntentRiskMetrics]:
    """Classify from projected exposure, never from BUY/SELL or purpose alone."""

    intent = request.intent
    mark = request.market_mark.mark_price
    multiplier = (
        request.instrument.contract_multiplier
        if request.instrument.asset_class == "OPTION"
        else Decimal("1")
    )
    multiplier = multiplier or Decimal("1")
    if intent.target_quantity is not None:
        requested_quantity = intent.target_quantity
    else:
        requested_quantity = intent.target_notional_usd / (mark * multiplier)  # type: ignore[operator]

    current_quantity = (
        request.current_position.quantity if request.current_position is not None else Decimal("0")
    )
    projected_quantity = current_quantity + _signed_delta(intent.side, requested_quantity)
    current_exposure = abs(current_quantity) * mark * multiplier
    projected_exposure = abs(projected_quantity) * mark * multiplier
    increases_risk = projected_exposure > current_exposure
    classification = (
        RiskClassification.RISK_INCREASING
        if increases_risk
        else RiskClassification.RISK_REDUCING
    )
    requested_cash = max(Decimal("0"), projected_exposure - current_exposure)
    if intent.target_notional_usd is not None and increases_risk:
        requested_cash = intent.target_notional_usd
    max_loss = requested_cash if request.instrument.asset_class == "OPTION" else None
    return classification, IntentRiskMetrics(
        increases_risk=increases_risk,
        requested_cash_usd=requested_cash,
        projected_exposure_usd=projected_exposure,
        max_contractual_loss_usd=max_loss,
        projected_quantity=projected_quantity,
        requested_quantity=requested_quantity,
    )
