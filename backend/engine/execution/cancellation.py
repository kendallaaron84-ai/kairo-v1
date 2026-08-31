from enum import StrEnum


class CancellationStatus(StrEnum):
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELED = "CANCELED"
    TOO_LATE_ALREADY_FILLED = "TOO_LATE_ALREADY_FILLED"


def resolve_cancellation(*, fully_filled: bool) -> CancellationStatus:
    return (
        CancellationStatus.TOO_LATE_ALREADY_FILLED
        if fully_filled
        else CancellationStatus.CANCELED
    )
