"""Small configured trading-calendar adapter for routine cash-market work.

This is deliberately narrower than the future S08 calendar engine.  It knows
the regular NSE cash session, weekends, and only the exchange holidays supplied
to it explicitly.  It does not invent an annual holiday list, Muhurat timings,
or exchange-declared special Saturday sessions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from dhruva.shared.errors import InvariantViolation
from dhruva.shared.time import SessionKind, TradingDay, TradingSession

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dhruva.shared.time import DateRangeLike

__all__ = ["ConfiguredNseCashCalendar"]

_IST = ZoneInfo("Asia/Kolkata")
_REGULAR_OPEN = time(9, 15)
_REGULAR_CLOSE = time(15, 30)
_DAYS_IN_WORK_WEEK = 5


@dataclass(frozen=True, slots=True)
class ConfiguredNseCashCalendar:
    """Regular NSE cash sessions with an explicit set of known closed dates.

    The empty production configuration is a conservative interim adapter: it
    skips Saturdays and Sundays, but treats every weekday as expected.  Thus an
    unconfigured weekday exchange holiday still refuses refresh instead of being
    silently accepted as fresh.  Callers with reviewed holiday evidence may
    supply those dates without changing refresh logic.
    """

    closed_dates: frozenset[date] = frozenset()

    def is_session(self, day: date) -> bool:
        """Return whether ``day`` is a configured regular cash session."""
        return day.weekday() < _DAYS_IN_WORK_WEEK and day not in self.closed_dates

    def session(self, day: TradingDay) -> TradingSession:
        """Return the regular 09:15-15:30 IST session as UTC instants."""
        if not self.is_session(day.on):
            raise InvariantViolation(
                "date is not a trading session on this calendar",
                day=day.iso,
                calendar=type(self).__name__,
            )
        opens_at = datetime.combine(day.on, _REGULAR_OPEN, tzinfo=_IST).astimezone(UTC)
        closes_at = datetime.combine(day.on, _REGULAR_CLOSE, tzinfo=_IST).astimezone(UTC)
        return TradingSession(
            day=day,
            opens_at=opens_at,
            closes_at=closes_at,
            kind=SessionKind.REGULAR,
        )

    def next_session(self, day: TradingDay) -> TradingDay:
        """Return the next configured session after ``day``."""
        return self.add_sessions(day, 1)

    def previous_session(self, day: TradingDay) -> TradingDay:
        """Return the previous configured session before ``day``."""
        return self.add_sessions(day, -1)

    def add_sessions(self, day: TradingDay, count: int) -> TradingDay:
        """Move by ``count`` configured sessions, skipping closed dates."""
        current = day.on
        step = 1 if count >= 0 else -1
        remaining = abs(count)
        while remaining:
            current += timedelta(days=step)
            if self.is_session(current):
                remaining -= 1
        return TradingDay(current)

    def sessions_between(self, span: DateRangeLike) -> Sequence[TradingDay]:
        """Return all configured sessions in the half-open date range."""
        sessions: list[TradingDay] = []
        current = span.start
        while current < span.end:
            if self.is_session(current):
                sessions.append(TradingDay(current))
            current += timedelta(days=1)
        return sessions
