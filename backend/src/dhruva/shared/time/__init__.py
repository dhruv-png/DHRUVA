"""Time as a first-class domain concept.

Three ideas, deliberately separate:

:class:`~dhruva.shared.time.clock.Clock`
    *What instant is it?* Injected, never read from the wall clock (ADR-011).

:class:`~dhruva.shared.time.trading_day.TradingCalendar`
    *Was the market open, and when?* A port; the Reference context implements it
    (ADR-046). Every session question goes through here rather than through date
    arithmetic.

:class:`~dhruva.shared.time.ranges.DateRange` / :class:`~dhruva.shared.time.ranges.TimeRange`
    *Which span?* Always half-open ``[start, end)``, so consecutive ranges tile
    without gaps or double-counting.

All instants are timezone-aware UTC. Display in Asia/Kolkata is a presentation
concern and happens at the edge (ADR-006).
"""

from __future__ import annotations

from dhruva.shared.time.clock import Clock, FrozenClock, SystemClock
from dhruva.shared.time.ranges import DateRange, TimeRange
from dhruva.shared.time.trading_day import (
    DateRangeLike,
    SessionKind,
    TradingCalendar,
    TradingDay,
    TradingSession,
)

__all__ = [
    "Clock",
    "DateRange",
    "DateRangeLike",
    "FrozenClock",
    "SessionKind",
    "SystemClock",
    "TimeRange",
    "TradingCalendar",
    "TradingDay",
    "TradingSession",
]
