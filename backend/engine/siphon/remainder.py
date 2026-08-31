from decimal import Decimal, ROUND_DOWN

from engine.siphon.models import SiphonBucket, SiphonPolicyConfig

CENT = Decimal("0.01")


def floor_cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_DOWN)


def allocate_exact_cents(
    qualified_profit_usd: Decimal, policy: SiphonPolicyConfig
) -> dict[SiphonBucket, Decimal]:
    qualified = floor_cents(qualified_profit_usd)
    reserve = floor_cents(qualified * policy.safety_reserve_pct)
    treasury = floor_cents(qualified * policy.target_treasury_pct)
    replication = floor_cents(qualified * policy.replication_pool_pct)
    replication += qualified - reserve - treasury - replication
    result = {
        SiphonBucket.SAFETY_RESERVE: reserve,
        SiphonBucket.TARGET_TREASURY: treasury,
        SiphonBucket.REPLICATION_POOL: replication,
    }
    if sum(result.values()) != qualified:
        raise ArithmeticError("cent allocation does not equal qualified profit")
    return result
