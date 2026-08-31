from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CounterfactualPolicy(StrEnum):
    V01_PROFIT_CEILING = "RISK-v0.1"
    V02_TRAILING_RATCHET = "RISK-CANDIDATE-v0.2"


class PolicyPathPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    session_pnl: Decimal
    realized_pnl_delta: Decimal = Decimal("0")
    commission_delta: Decimal = Field(default=Decimal("0"), ge=0)

    @model_validator(mode="after")
    def timestamp_is_aware(self) -> "PolicyPathPoint":
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("policy path timestamps must be timezone-aware")
        return self


class PolicySessionPath(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str = Field(min_length=1)
    points: tuple[PolicyPathPoint, ...]

    @model_validator(mode="after")
    def points_are_strictly_chronological(self) -> "PolicySessionPath":
        timestamps = [point.timestamp for point in self.points]
        if any(current <= previous for previous, current in zip(timestamps, timestamps[1:])):
            raise ValueError("policy path points must be strictly chronological")
        return self


class PolicyMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    policy: CounterfactualPolicy
    net_realized_profit: Decimal
    max_drawdown: Decimal = Field(ge=0)
    peak_profit_capture_ratio: Decimal | None
    lock_trigger_frequency: Decimal = Field(ge=0, le=1)
    sessions_evaluated: int = Field(ge=0)
    sessions_locked: int = Field(ge=0)


class CounterfactualComparison(BaseModel):
    model_config = ConfigDict(frozen=True)

    baseline_v01: PolicyMetrics
    candidate_v02: PolicyMetrics


class _SessionOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    net_realized_profit: Decimal
    max_drawdown: Decimal
    peak_capture_ratio: Decimal | None
    locked: bool


def compare_policies(
    sessions: tuple[PolicySessionPath, ...]
) -> CounterfactualComparison:
    """Evaluate frozen v0.1 and candidate v0.2 without writing runtime state."""

    return CounterfactualComparison(
        baseline_v01=_aggregate(CounterfactualPolicy.V01_PROFIT_CEILING, sessions),
        candidate_v02=_aggregate(CounterfactualPolicy.V02_TRAILING_RATCHET, sessions),
    )


def _aggregate(
    policy: CounterfactualPolicy, sessions: tuple[PolicySessionPath, ...]
) -> PolicyMetrics:
    outcomes = tuple(_evaluate_session(policy, session) for session in sessions)
    count = len(outcomes)
    ratios = [
        outcome.peak_capture_ratio
        for outcome in outcomes
        if outcome.peak_capture_ratio is not None
    ]
    locked = sum(outcome.locked for outcome in outcomes)
    return PolicyMetrics(
        policy=policy,
        net_realized_profit=sum(
            (outcome.net_realized_profit for outcome in outcomes), Decimal("0")
        ),
        max_drawdown=max(
            (outcome.max_drawdown for outcome in outcomes), default=Decimal("0")
        ),
        peak_profit_capture_ratio=(
            sum(ratios, Decimal("0")) / Decimal(len(ratios)) if ratios else None
        ),
        lock_trigger_frequency=(Decimal(locked) / Decimal(count) if count else Decimal("0")),
        sessions_evaluated=count,
        sessions_locked=locked,
    )


def _evaluate_session(
    policy: CounterfactualPolicy, session: PolicySessionPath
) -> _SessionOutcome:
    if not session.points:
        return _SessionOutcome(
            net_realized_profit=Decimal("0"),
            max_drawdown=Decimal("0"),
            peak_capture_ratio=None,
            locked=False,
        )
    peak = session.points[0].session_pnl
    maximum_positive = max(
        (point.session_pnl for point in session.points), default=Decimal("0")
    )
    maximum_drawdown = Decimal("0")
    net_realized = Decimal("0")
    terminal_pnl = session.points[-1].session_pnl
    locked = False
    ratchet_active = False
    for point in session.points:
        peak = max(peak, point.session_pnl)
        maximum_drawdown = max(maximum_drawdown, peak - point.session_pnl)
        net_realized += point.realized_pnl_delta - point.commission_delta
        if policy is CounterfactualPolicy.V01_PROFIT_CEILING:
            locked = point.session_pnl >= Decimal("20")
        else:
            ratchet_active = ratchet_active or peak >= Decimal("20")
            locked = ratchet_active and point.session_pnl <= Decimal("0.80") * peak
        if locked:
            terminal_pnl = point.session_pnl
            break
    ratio = (
        terminal_pnl / maximum_positive
        if maximum_positive > 0
        else None
    )
    return _SessionOutcome(
        net_realized_profit=net_realized,
        max_drawdown=maximum_drawdown,
        peak_capture_ratio=ratio,
        locked=locked,
    )
