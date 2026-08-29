from dataclasses import dataclass
from decimal import Decimal

from engine.trust.models import (
    EquityPoint,
    EvidenceStatus,
    ExecutionEvidence,
    FactorScore,
    TrustEvidenceBundle,
)


TRUST_V01_FACTORS = (
    "risk_adjusted_outcomes",
    "drawdown_control",
    "execution_quality",
    "excursion_efficiency",
    "strategy_discipline",
    "regime_consistency",
)

HUNDRED = Decimal("100")


def _clamp(score: Decimal) -> Decimal:
    return min(HUNDRED, max(Decimal("0"), score))


def _missing(factor: str, weight: Decimal, reason: str) -> FactorScore:
    return FactorScore(
        factor=factor,
        status=EvidenceStatus.INSUFFICIENT_EVIDENCE,
        score=None,
        weight=weight,
        reason=reason,
    )


@dataclass(frozen=True)
class DrawdownResult:
    amount: Decimal
    percent: Decimal


def chronological_drawdown(points: list[EquityPoint]) -> DrawdownResult | None:
    if not points:
        return None
    ordered = sorted(points, key=lambda point: point.timestamp)
    peak = ordered[0].equity
    max_amount = Decimal("0")
    max_percent = Decimal("0")
    for point in ordered:
        if point.equity > peak:
            peak = point.equity
            continue
        amount = peak - point.equity
        percent = amount / peak if peak > 0 else Decimal("0")
        if percent > max_percent:
            max_amount = amount
            max_percent = percent
    return DrawdownResult(amount=max_amount, percent=max_percent)


def adverse_slippage_usd(execution: ExecutionEvidence) -> Decimal | None:
    if execution.reference_price is None:
        return None
    if execution.side.value == "BUY":
        adverse_per_unit = max(
            Decimal("0"), execution.fill_price - execution.reference_price
        )
    else:
        adverse_per_unit = max(
            Decimal("0"), execution.reference_price - execution.fill_price
        )
    return adverse_per_unit * execution.quantity * execution.contract_multiplier


def score_risk_adjusted_outcomes(
    evidence: TrustEvidenceBundle, weight: Decimal
) -> FactorScore:
    factor = "risk_adjusted_outcomes"
    trades = evidence.closed_trades
    if not trades:
        return _missing(factor, weight, "no closed-trade evidence")
    if any(
        trade.realized_pnl_usd is None
        or trade.planned_risk_usd is None
        or trade.settlement_verified is not True
        for trade in trades
    ):
        return _missing(factor, weight, "planned-risk or settlement evidence is missing")
    r_values = [
        trade.realized_pnl_usd / trade.planned_risk_usd  # type: ignore[operator]
        for trade in trades
    ]
    average_r = sum(r_values, start=Decimal("0")) / Decimal(len(r_values))
    return FactorScore(
        factor=factor,
        status=EvidenceStatus.AVAILABLE,
        score=_clamp(Decimal("50") + Decimal("25") * average_r),
        weight=weight,
        evidence_count=len(trades),
    )


def score_drawdown_control(
    evidence: TrustEvidenceBundle, weight: Decimal
) -> FactorScore:
    factor = "drawdown_control"
    result = chronological_drawdown(list(evidence.equity_curve))
    if result is None or len(evidence.equity_curve) < 2:
        return _missing(factor, weight, "chronological equity-path evidence is missing")
    return FactorScore(
        factor=factor,
        status=EvidenceStatus.AVAILABLE,
        score=_clamp(HUNDRED * (Decimal("1") - result.percent)),
        weight=weight,
        evidence_count=len(evidence.equity_curve),
        reason=f"maximum_drawdown_usd={result.amount}",
    )


def score_execution_quality(
    evidence: TrustEvidenceBundle, weight: Decimal
) -> FactorScore:
    factor = "execution_quality"
    executions = evidence.executions
    if not executions or any(item.reference_price is None for item in executions):
        return _missing(factor, weight, "reference-price execution evidence is missing")
    adverse = [adverse_slippage_usd(item) for item in executions]
    reference_notional = sum(
        (
            item.reference_price * item.quantity * item.contract_multiplier  # type: ignore[operator]
            for item in executions
        ),
        start=Decimal("0"),
    )
    if reference_notional <= 0 or any(value is None for value in adverse):
        return _missing(factor, weight, "execution notional cannot be established")
    adverse_total = sum((value for value in adverse if value is not None), start=Decimal("0"))
    return FactorScore(
        factor=factor,
        status=EvidenceStatus.AVAILABLE,
        score=_clamp(HUNDRED * (Decimal("1") - adverse_total / reference_notional)),
        weight=weight,
        evidence_count=len(executions),
        reason=f"adverse_slippage_usd={adverse_total}",
    )


def score_excursion_efficiency(
    evidence: TrustEvidenceBundle, weight: Decimal
) -> FactorScore:
    factor = "excursion_efficiency"
    trades = evidence.closed_trades
    if not trades or any(trade.mfe_r is None or trade.mae_r is None for trade in trades):
        return _missing(factor, weight, "MFE/MAE path evidence is missing")
    average_mfe = sum((trade.mfe_r for trade in trades), start=Decimal("0")) / Decimal(
        len(trades)
    )
    average_mae = sum((trade.mae_r for trade in trades), start=Decimal("0")) / Decimal(
        len(trades)
    )
    denominator = average_mfe + average_mae
    if denominator == 0:
        return _missing(factor, weight, "MFE/MAE efficiency denominator is zero")
    return FactorScore(
        factor=factor,
        status=EvidenceStatus.AVAILABLE,
        score=_clamp(HUNDRED * average_mfe / denominator),
        weight=weight,
        evidence_count=len(trades),
    )


def score_strategy_discipline(
    evidence: TrustEvidenceBundle, weight: Decimal
) -> FactorScore:
    factor = "strategy_discipline"
    compliance = [trade.strategy_compliant for trade in evidence.closed_trades]
    if not compliance or any(value is None for value in compliance):
        return _missing(factor, weight, "strategy-compliance evidence is missing")
    compliant = sum(1 for value in compliance if value is True)
    good = compliant + evidence.governor.authorized_intents
    total = good + (len(compliance) - compliant) + evidence.governor.rejected_intents
    if total == 0:
        return _missing(factor, weight, "discipline observations are missing")
    return FactorScore(
        factor=factor,
        status=EvidenceStatus.AVAILABLE,
        score=HUNDRED * Decimal(good) / Decimal(total),
        weight=weight,
        evidence_count=total,
        reason=f"governor_rejections={evidence.governor.rejected_intents}",
    )


def score_regime_consistency(
    evidence: TrustEvidenceBundle, weight: Decimal
) -> FactorScore:
    factor = "regime_consistency"
    trades = evidence.closed_trades
    if not trades or any(
        trade.regime is None or trade.realized_pnl_usd is None for trade in trades
    ):
        return _missing(factor, weight, "regime telemetry is missing")
    profitable = sum(1 for trade in trades if trade.realized_pnl_usd >= 0)
    return FactorScore(
        factor=factor,
        status=EvidenceStatus.AVAILABLE,
        score=HUNDRED * Decimal(profitable) / Decimal(len(trades)),
        weight=weight,
        evidence_count=len(trades),
    )


def compute_factor_scores(
    evidence: TrustEvidenceBundle, weights: dict[str, Decimal]
) -> tuple[FactorScore, ...]:
    scorers = (
        score_risk_adjusted_outcomes,
        score_drawdown_control,
        score_execution_quality,
        score_excursion_efficiency,
        score_strategy_discipline,
        score_regime_consistency,
    )
    return tuple(scorer(evidence, weights[scorer.__name__.removeprefix("score_")]) for scorer in scorers)


def weighted_score(
    factors: tuple[FactorScore, ...], required_factors: tuple[str, ...]
) -> Decimal | None:
    required = set(required_factors)
    if any(
        factor.factor in required and factor.status is not EvidenceStatus.AVAILABLE
        for factor in factors
    ):
        return None
    available = [
        factor
        for factor in factors
        if factor.status is EvidenceStatus.AVAILABLE and factor.score is not None
    ]
    weight_total = sum((factor.weight for factor in available), start=Decimal("0"))
    if weight_total <= 0:
        return None
    # NOT_APPLICABLE optional factors are excluded, renormalizing remaining weights to 1.0.
    return sum(
        (factor.score * factor.weight / weight_total for factor in available),
        start=Decimal("0"),
    )
