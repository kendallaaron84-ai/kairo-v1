from datetime import date, datetime, time
from zoneinfo import ZoneInfo


class SessionCalendarResolver:
    """Frozen 2026 US equities RTH authority; timezone conversion uses IANA DST."""

    calendar_name = "XNYS"
    calendar_version = "CAL-US-EQUITIES-2026-v1"
    eastern = ZoneInfo("America/New_York")
    holidays = frozenset(
        date.fromisoformat(item) for item in (
            "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
            "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
            "2026-11-26", "2026-12-25",
        )
    )
    early_closes = {
        date(2026, 7, 2): time(13, 0),
        date(2026, 11, 27): time(13, 0),
        date(2026, 12, 24): time(13, 0),
    }

    def session_bounds(self, session_date: date) -> tuple[datetime, datetime] | None:
        if session_date.weekday() >= 5 or session_date in self.holidays:
            return None
        opened = datetime.combine(session_date, time(9, 30), self.eastern)
        closed = datetime.combine(session_date, self.early_closes.get(session_date, time(16, 0)), self.eastern)
        return opened, closed

    def contains_completed_interval(self, start_at: datetime, completed_at: datetime) -> bool:
        if start_at.tzinfo is None or completed_at.tzinfo is None:
            raise ValueError("calendar timestamps must be timezone-aware")
        local_start = start_at.astimezone(self.eastern)
        local_done = completed_at.astimezone(self.eastern)
        bounds = self.session_bounds(local_start.date())
        return bool(bounds and bounds[0] <= local_start < bounds[1] and local_start < local_done <= bounds[1])
