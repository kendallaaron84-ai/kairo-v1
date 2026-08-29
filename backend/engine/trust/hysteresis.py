from decimal import Decimal

from app.domain.enums import AutonomyTier
from engine.trust.models import SafetyEligibility


WINDOWS = {20: "W_20", 50: "W_50"}
TIER_ORDER = (
    AutonomyTier.APPRENTICE.value,
    AutonomyTier.GUARDED.value,
    AutonomyTier.CAPITAL_BUILDER.value,
)


def latest_window[T](facts: tuple[T, ...], size: int) -> tuple[T, ...]:
    if size not in WINDOWS:
        raise ValueError("TRUST-v0.1 supports only W_20 and W_50")
    return facts[-size:]


def recommend_autonomy_tier(
    *,
    current_tier: str,
    eligibility: SafetyEligibility,
    score: Decimal | None,
    trade_count: int,
    window_size: int,
    promotion_thresholds: dict[str, Decimal],
    demotion_thresholds: dict[str, Decimal],
) -> str:
    normalized = current_tier if current_tier in TIER_ORDER else AutonomyTier.APPRENTICE.value
    index = TIER_ORDER.index(normalized)
    if eligibility is SafetyEligibility.DISQUALIFIED:
        return AutonomyTier.APPRENTICE.value
    if eligibility is not SafetyEligibility.ELIGIBLE or score is None:
        return normalized

    demotion_floor = demotion_thresholds.get(normalized)
    if demotion_floor is not None and score < demotion_floor and index > 0:
        return TIER_ORDER[index - 1]

    if trade_count < window_size or index >= len(TIER_ORDER) - 1:
        return normalized
    next_tier = TIER_ORDER[index + 1]
    threshold = promotion_thresholds.get(next_tier)
    if threshold is not None and score >= threshold:
        return next_tier
    return normalized
