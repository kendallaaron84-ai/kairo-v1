from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


class SessionCalendarResolver:
    """Frozen 2026 US equities RTH authority; timezone conversion uses IANA DST."""

    calendar_name = "XNYS"
    calendar_version = "CAL-US-EQUITIES-2026-v1"
    eastern = ZoneInfo("America/New_York")
    holidays: frozenset[date]
    early_closes: dict[date, time]

    def __init__(self) -> None:
        holidays: set[date] = set()
        early: dict[date, time] = {}
        for year in range(2018, 2027):
            holidays.update(self._holidays_for(year))
            thanksgiving = self._nth_weekday(year, 11, 3, 4)
            early[thanksgiving + timedelta(days=1)] = time(13, 0)
            christmas_eve = date(year, 12, 24)
            if christmas_eve.weekday() < 5 and christmas_eve not in holidays:
                early[christmas_eve] = time(13, 0)
            july4 = date(year, 7, 4)
            prior = july4 - timedelta(days=1)
            if prior.weekday() < 5 and prior not in holidays:
                early[prior] = time(13, 0)
            elif july4.weekday() == 5:
                prior = july4 - timedelta(days=2)
                if prior not in holidays:
                    early[prior] = time(13, 0)
        self.holidays = frozenset(holidays)
        self.early_closes = early

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

    def sessions(self, start_date: date, end_date: date) -> tuple[tuple[date, datetime, datetime], ...]:
        if end_date < start_date:
            raise ValueError("session end date cannot precede start date")
        rows = []
        current = start_date
        while current <= end_date:
            bounds = self.session_bounds(current)
            if bounds is not None:
                rows.append((current, bounds[0], bounds[1]))
            current += timedelta(days=1)
        return tuple(rows)

    @classmethod
    def _holidays_for(cls, year: int) -> set[date]:
        values = {
            cls._observed(date(year, 1, 1)),
            cls._nth_weekday(year, 1, 0, 3),
            cls._nth_weekday(year, 2, 0, 3),
            cls._good_friday(year),
            cls._last_weekday(year, 5, 0),
            cls._observed(date(year, 7, 4)),
            cls._nth_weekday(year, 9, 0, 1),
            cls._nth_weekday(year, 11, 3, 4),
            cls._observed(date(year, 12, 25)),
        }
        if year >= 2022:
            values.add(cls._observed(date(year, 6, 19)))
        return values

    @staticmethod
    def _observed(value: date) -> date:
        if value.weekday() == 5:
            return value - timedelta(days=1)
        if value.weekday() == 6:
            return value + timedelta(days=1)
        return value

    @staticmethod
    def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (occurrence - 1))

    @staticmethod
    def _last_weekday(year: int, month: int, weekday: int) -> date:
        next_month = date(year + (month == 12), month % 12 + 1, 1)
        last = next_month - timedelta(days=1)
        return last - timedelta(days=(last.weekday() - weekday) % 7)

    @staticmethod
    def _good_friday(year: int) -> date:
        # Anonymous Gregorian computus; exchange holiday is two days before Easter.
        a = year % 19; b = year // 100; c = year % 100; d = b // 4; e = b % 4
        f = (b + 8) // 25; g = (b - f + 1) // 3; h = (19 * a + b - d - g + 15) % 30
        i = c // 4; k = c % 4; l = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * l) // 451
        month = (h + l - 7 * m + 114) // 31
        day = (h + l - 7 * m + 114) % 31 + 1
        return date(year, month, day) - timedelta(days=2)
