from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class RegimeObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_date: date
    market_return_pct: Decimal
    vix_level: Decimal | None = None
    rate_change_bps: Decimal | None = None
    event_count: int = Field(default=0, ge=0)


class RegimePolicyV1:
    """Versioned session labels; labels overlap intentionally."""

    version = "REGIME-POLICY-MULTI-v1"
    label_order = (
        "BULL",
        "BEAR",
        "HIGH_VOL",
        "LOW_VOL",
        "RATE_SHOCK",
        "EVENT_HEAVY",
        "SIDEWAYS",
    )

    def classify(self, observation: RegimeObservation) -> tuple[str, ...]:
        labels: set[str] = set()
        if observation.market_return_pct >= Decimal("1.00"):
            labels.add("BULL")
        elif observation.market_return_pct <= Decimal("-1.00"):
            labels.add("BEAR")
        else:
            labels.add("SIDEWAYS")
        if observation.vix_level is not None:
            if observation.vix_level >= Decimal("25.00"):
                labels.add("HIGH_VOL")
            elif observation.vix_level <= Decimal("15.00"):
                labels.add("LOW_VOL")
        if observation.rate_change_bps is not None and abs(observation.rate_change_bps) >= Decimal("25.00"):
            labels.add("RATE_SHOCK")
        if observation.event_count >= 3:
            labels.add("EVENT_HEAVY")
        return tuple(label for label in self.label_order if label in labels)
