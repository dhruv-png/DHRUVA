"""Schedules expressed in trading-day terms (ADR-066).

Pure domain logic. No Celery, no clock, no I/O -- everything here is a function
of its arguments and of a :class:`TradingCalendar`, which is a port. That is the
whole point: a calendar-resolved schedule can be asserted against Diwali without
waiting for Diwali.

Why not cron
------------
``0 9 * * 1-5`` is wrong on every exchange holiday, wrong on the occasional
Saturday session, and was wrong about expiry conventions the day they changed.
Worse, it is a *string*: no test will ever contradict it, so the defect is
latent until a holiday, which is precisely when nobody is watching.

A job here declares a **session-relative moment** -- ``market_open + 5min``,
``market_close - 15min`` -- and the calendar decides both whether the day
qualifies and what wall-clock instant that is. A day that does not qualify
produces no occurrence, and ADR-066 is explicit that this is the intended
behaviour rather than a failed schedule.

The two-part shape
------------------
:meth:`TradingDaySchedule.at` takes a :class:`TradingSession` and needs no
calendar at all, so the arithmetic -- the part that can be subtly wrong -- is
testable without stubbing anything. The calendar only appears in
:meth:`occurrences` and :meth:`next_after`, which answer "which sessions?" and
delegate the rest back to :meth:`at`.

Bounded by construction
-----------------------
Neither method searches forward indefinitely. A caller supplies the span to look
in, and a schedule that never fires inside it returns nothing rather than
looping. A beat that asked "when next?" without bounds would hang on a schedule
restricted to Muhurat sessions, and would hang in the scheduler rather than
anywhere a stack trace would be read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from dhruva.shared.errors import ValidationError
from dhruva.shared.time import SessionKind

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

    from dhruva.shared.time import (
        DateRange,
        TradingCalendar,
        TradingDay,
        TradingSession,
    )

__all__ = ["MAX_OFFSET", "SessionAnchor", "TradingDaySchedule"]

#: The largest offset a session-relative moment may carry.
#:
#: Not a tuning parameter. Beyond a day the moment belongs to a *different*
#: session than the one it is anchored to, and the schedule is then lying about
#: which session it follows: "market_close + 30h" on a Friday is Sunday, which is
#: not a session at all, while on a Monday it is Tuesday lunchtime. Both are
#: almost certainly not what the author meant, and neither reads that way.
#:
#: Work genuinely detached from a session -- log rotation, nightly pruning -- is
#: what ADR-066 leaves cron for. It is not what this type is for.
MAX_OFFSET: Final = timedelta(days=1)


class SessionAnchor(StrEnum):
    """The instant a schedule is measured from.

    Two, because the port exposes two: a :class:`TradingSession` is
    ``[opens_at, closes_at)`` and nothing else. ADR-066's prose says
    ``session_end`` in one example and ``market_close`` in another; they are the
    same instant, and inventing a third anchor to match the prose would create a
    distinction the calendar cannot honour.
    """

    MARKET_OPEN = "market_open"
    MARKET_CLOSE = "market_close"


@dataclass(frozen=True, slots=True)
class TradingDaySchedule:
    """When a job runs, expressed relative to a trading session.

    Attributes
    ----------
    anchor
        Which end of the session the offset is measured from.
    offset
        How far from the anchor, positive after and negative before. A negative
        offset against ``MARKET_OPEN`` is the ordinary way to express pre-open
        work, and is not treated as an error.
    kinds
        Which session kinds qualify. Defaults to every kind, because the common
        case is a job that should run whenever the market is open -- including on
        Muhurat, where forgetting it means a settlement job silently skipping the
        one session everybody remembers.

    Examples
    --------
    >>> from datetime import timedelta
    >>> warm_up = TradingDaySchedule(
    ...     anchor=SessionAnchor.MARKET_OPEN, offset=-timedelta(minutes=30)
    ... )
    >>> warm_up.describe()
    'market_open - 0:30:00'
    """

    anchor: SessionAnchor
    offset: timedelta = timedelta(0)
    kinds: frozenset[SessionKind] = field(
        default_factory=lambda: frozenset(SessionKind), compare=True
    )

    def __post_init__(self) -> None:
        """Refuse a schedule that could not mean what it says."""
        if not self.kinds:
            raise ValidationError(
                "a schedule with no session kinds would never run; omit the "
                "argument to accept every kind, or name the ones you want",
                anchor=str(self.anchor),
            )
        if abs(self.offset) > MAX_OFFSET:
            raise ValidationError(
                "an offset beyond a day belongs to a different session than the "
                "one it is anchored to; use cron for work detached from a session "
                "(ADR-066)",
                offset=str(self.offset),
                maximum=str(MAX_OFFSET),
            )

    def describe(self) -> str:
        """Return a human-readable form, for a log line or an operator listing."""
        if not self.offset:
            return str(self.anchor)
        sign = "-" if self.offset < timedelta(0) else "+"
        return f"{self.anchor} {sign} {abs(self.offset)}"

    def accepts(self, session: TradingSession) -> bool:
        """Return whether this session is one this schedule runs on."""
        return session.kind in self.kinds

    def at(self, session: TradingSession) -> datetime | None:
        """Return the instant this schedule fires in ``session``, or ``None``.

        ``None`` means the session does not qualify -- a Muhurat session for a
        schedule restricted to regular ones. ADR-066: a moment that does not
        occur today simply does not run, and that is not a failure.

        Pure. Takes a session rather than a calendar, so the arithmetic can be
        asserted without stubbing anything, and so a caller that already holds a
        session does not fetch it twice.
        """
        if not self.accepts(session):
            return None
        anchor = session.opens_at if self.anchor is SessionAnchor.MARKET_OPEN else session.closes_at
        return anchor + self.offset

    def occurrences(self, span: DateRange, calendar: TradingCalendar) -> Sequence[datetime]:
        """Return every instant this schedule fires on, for sessions inside ``span``.

        Ordered, because the sessions the calendar returns are ordered and the
        same offset applied to each preserves that. A caller planning a day's
        work relies on it, and a test asserts it rather than trusting it.

        Note that the span selects *sessions by date*, not firing instants: a
        schedule anchored to ``market_close + 6h`` on the last session in the span
        fires on the following calendar date, and that occurrence is included.
        Filtering by firing instant instead would silently drop it.
        """
        return tuple(self._fire_times(calendar.sessions_between(span), calendar))

    def next_after(
        self, instant: datetime, span: DateRange, calendar: TradingCalendar
    ) -> datetime | None:
        """Return the first firing strictly after ``instant``, or ``None``.

        Strictly after, so a beat that has just run at exactly this instant does
        not immediately find the same occurrence again and run it twice.

        The span is required rather than searched for. An unbounded "when next?"
        hangs forever on a schedule that never qualifies -- one restricted to
        Muhurat sessions, asked in March -- and it hangs inside the scheduler,
        where nobody sees a stack trace.
        """
        return next((fire for fire in self.occurrences(span, calendar) if fire > instant), None)

    def _fire_times(
        self, days: Iterable[TradingDay], calendar: TradingCalendar
    ) -> Iterable[datetime]:
        """Yield the firing instant of each qualifying day."""
        for day in days:
            fire = self.at(calendar.session(day))
            if fire is not None:
                yield fire
