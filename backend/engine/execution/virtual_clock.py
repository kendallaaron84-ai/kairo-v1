from collections import defaultdict
from datetime import datetime
from uuid import NAMESPACE_DNS, UUID, uuid5


class VirtualClock:
    def __init__(self, initial_time: datetime) -> None:
        self._require_aware(initial_time)
        self._current_time = initial_time

    @staticmethod
    def _require_aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("VirtualClock requires timezone-aware datetimes")

    def advance_to(self, new_time: datetime) -> None:
        self._require_aware(new_time)
        if new_time < self._current_time:
            raise ValueError(
                f"VirtualClock cannot move backward: {new_time} < {self._current_time}"
            )
        self._current_time = new_time

    def now(self) -> datetime:
        return self._current_time


class ReplayIdentityFactory:
    """Causal UUIDv5 identities with independent ordinals per financial record type."""

    def __init__(self, session_id: str) -> None:
        if not session_id:
            raise ValueError("replay session_id is required")
        self.session_id = session_id
        self._namespace = uuid5(NAMESPACE_DNS, f"kairo-replay:{session_id}")
        self._sequences: dict[tuple[str, str, str], int] = defaultdict(int)

    def generate_id(
        self,
        record_type: str,
        instrument_id: UUID,
        timestamp: datetime,
        *,
        parent_id: UUID | str | None = None,
        ordinal_within_parent: int | None = None,
    ) -> UUID:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("deterministic identity timestamps must be timezone-aware")
        parent = str(parent_id or "ROOT")
        key = (record_type, parent, str(instrument_id))
        if ordinal_within_parent is None:
            self._sequences[key] += 1
            ordinal = self._sequences[key]
        else:
            if ordinal_within_parent < 1:
                raise ValueError("causal ordinal must be positive")
            ordinal = ordinal_within_parent
        seed = ":".join(
            (
                self.session_id,
                record_type,
                parent,
                str(instrument_id),
                timestamp.isoformat(),
                str(ordinal),
            )
        )
        return uuid5(self._namespace, seed)
