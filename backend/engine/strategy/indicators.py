from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


EMA_PERIOD = 9
EMA_ALPHA = Decimal("0.20")
FIRST_READY_CLOSE = 10


class EMAValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    close_number: int = Field(gt=0)
    close: Decimal = Field(gt=0)
    ema: Decimal | None = Field(default=None, gt=0)
    ready: bool


class PrototypeEMA9:
    """Decimal implementation of the prototype's exact SMA-seeded EMA-9."""

    def __init__(self) -> None:
        self._closes: list[Decimal] = []
        self._values: list[EMAValue] = []

    @property
    def values(self) -> tuple[EMAValue, ...]:
        return tuple(self._values)

    @property
    def ready(self) -> bool:
        return len(self._values) >= FIRST_READY_CLOSE

    def append(self, close: Decimal) -> EMAValue:
        if not isinstance(close, Decimal):
            raise TypeError("EMA closes must be Decimal values")
        if close <= 0:
            raise ValueError("EMA close must be positive")

        self._closes.append(close)
        close_number = len(self._closes)
        if close_number < EMA_PERIOD:
            ema = None
        elif close_number == EMA_PERIOD:
            ema = sum(self._closes, start=Decimal("0")) / Decimal(EMA_PERIOD)
        else:
            previous = self._values[-1].ema
            if previous is None:
                raise RuntimeError("EMA recursive state is unavailable")
            ema = close * EMA_ALPHA + previous * (Decimal("1") - EMA_ALPHA)

        value = EMAValue(
            close_number=close_number,
            close=close,
            ema=ema,
            ready=close_number >= FIRST_READY_CLOSE,
        )
        self._values.append(value)
        return value
