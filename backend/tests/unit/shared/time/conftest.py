"""A minimal calendar for testing the TradingDay contract.

Deliberately not the real one -- S08 owns that. This stands in for it so the port
can be exercised without depending on holidays that do not exist yet.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta

import pytest

from dhruva.shared.time import SessionKind, TradingDay, TradingSession

#: Indian Standard Time is UTC+5:30 and observes no daylight saving. Sessions are
#: nonetheless stored as UTC instants so that the schema does not bake that in.
_IST_OFFSET = timedelta(hours=5, minutes=30)


class WeekdayCalendar:
    """Every weekday is a session; weekends are not. No holidays."""

    def __init__(self, *, holidays: frozenset[date] = frozenset()) -> None:
        """Optionally exclude specific dates, to exercise the holiday path."""
        self._holidays = holidays

    def is_session(self, day: date) -> bool:
        """Return whether the market was open."""
        return day.weekday() < 5 and day not in self._holidays

    def session(self, day: TradingDay) -> TradingSession:
        """Return 09:15-15:30 IST expressed as UTC instants."""
        opens = datetime.combine(day.on, time(9, 15), tzinfo=UTC) - _IST_OFFSET
        closes = datetime.combine(day.on, time(15, 30), tzinfo=UTC) - _IST_OFFSET
        return TradingSession(day=day, opens_at=opens, closes_at=closes)

    def next_session(self, day: TradingDay) -> TradingDay:
        """Return the next session after ``day``."""
        return self.add_sessions(day, 1)

    def previous_session(self, day: TradingDay) -> TradingDay:
        """Return the session before ``day``."""
        return self.add_sessions(day, -1)

    def add_sessions(self, day: TradingDay, count: int) -> TradingDay:
        """Move ``count`` sessions, skipping non-sessions."""
        step = 1 if count >= 0 else -1
        current = day.on
        remaining = abs(count)
        while remaining:
            current += timedelta(days=step)
            if self.is_session(current):
                remaining -= 1
        return TradingDay(current)

    def sessions_between(self, span: object) -> Sequence[TradingDay]:
        """Return every session in the half-open range."""
        start = span.start  # type: ignore[attr-defined]
        end = span.end  # type: ignore[attr-defined]
        days: list[TradingDay] = []
        current = start
        while current < end:
            if self.is_session(current):
                days.append(TradingDay(current))
            current += timedelta(days=1)
        return days


@pytest.fixture
def calendar() -> WeekdayCalendar:
    """Return a calendar where weekdays are sessions."""
    return WeekdayCalendar()


@pytest.fixture
def calendar_with_holiday() -> WeekdayCalendar:
    """Return a calendar with 2026-08-15 (Independence Day) closed."""
    return WeekdayCalendar(holidays=frozenset({date(2026, 8, 14)}))


@pytest.fixture
def muhurat_session() -> TradingSession:
    """Return a short ceremonial session, to exercise the partial-session path."""
    day = TradingDay(date(2026, 11, 8))
    return TradingSession(
        day=day,
        opens_at=datetime(2026, 11, 8, 13, 15, tzinfo=UTC),
        closes_at=datetime(2026, 11, 8, 14, 15, tzinfo=UTC),
        kind=SessionKind.MUHURAT,
    )
