"""The Celery beat adapter for trading-day schedules (ADR-066).

Translation, and nothing else. Whether a job runs today and at what instant is
decided by :class:`TradingDaySchedule`, which is pure domain logic and knows
nothing about Celery. This class answers the one question beat actually asks --
``is_due(last_run_at)`` -- by delegating to that policy and converting the answer
into the shape Celery expects.

Keeping the decision out of here is what ADR-066 means by "schedule resolution is
domain logic and lives outside Celery, so replacing Celery later is an adapter
swap rather than a rewrite of every schedule". If any part of *when* moved into
this file, that sentence would stop being true.

Why beat is asked again long before the job is due
---------------------------------------------------
``is_due`` returns how long to sleep as well as whether to run. Returning the
full interval would be correct and unwise: beat would sleep through a calendar
change, a clock correction or a redeployment that happened in between. The
sleep is capped, so a schedule is re-evaluated against the calendar regularly
and a holiday added this morning takes effect this morning.
"""

from __future__ import annotations

from datetime import UTC, timedelta
from typing import TYPE_CHECKING, Any, Final

from celery.schedules import BaseSchedule, schedstate

from dhruva.shared.errors import ValidationError
from dhruva.shared.time import DateRange

if TYPE_CHECKING:
    from datetime import datetime

    from dhruva.contexts.platform.domain.scheduling import TradingDaySchedule
    from dhruva.shared.time import TradingCalendar

__all__ = ["MAX_SLEEP", "SEARCH_HORIZON", "TradingDayBeatSchedule"]

#: How far ahead the calendar is consulted when looking for the next firing.
#:
#: Bounded because :meth:`TradingDaySchedule.next_after` requires a span, and
#: because an unbounded search would walk forever for a schedule that never
#: qualifies. Two weeks comfortably spans the longest sequence of consecutive
#: non-sessions the NSE calendar produces -- a weekend either side of a
#: multi-day festival -- with room to spare.
SEARCH_HORIZON: Final = timedelta(days=14)

#: The longest beat will sleep before re-evaluating, regardless of how far away
#: the next firing is.
#:
#: Not a performance knob. Beat holds the calendar it was given, and a schedule
#: that slept until Friday would not notice a holiday declared on Wednesday, a
#: clock correction, or a calendar redeployed underneath it. Five minutes of
#: re-evaluation costs nothing and removes a class of "the job did not run and
#: nothing logged anything" incidents.
MAX_SLEEP: Final = timedelta(minutes=5)

#: How long to wait when the schedule has no firing anywhere in the horizon --
#: a Muhurat-only job in March. Longer than :data:`MAX_SLEEP` would be
#: reasonable, and identical is simpler: the same re-evaluation cadence covers
#: both, and there is one number to reason about rather than two.
IDLE_SLEEP: Final = MAX_SLEEP


class TradingDayBeatSchedule(BaseSchedule):  # type: ignore[misc]  # celery ships no stubs
    """A Celery beat schedule that fires on trading-session moments.

    Parameters
    ----------
    schedule
        The domain policy. Owns every decision about *when*.
    calendar
        The port the policy resolves through. Injected rather than constructed,
        so beat in a test uses a stub calendar and asserts on Diwali without
        waiting for Diwali.
    nowfun
        Supplied to :class:`BaseSchedule`; Celery calls it for the current
        instant. Passed through rather than read here, which is what lets a test
        freeze it (ADR-011).

    Notes
    -----
    Beat's contract is stateless in one direction and stateful in the other: it
    hands back ``last_run_at`` and expects a verdict. That is enough here,
    because a session-relative moment is a pure function of the calendar -- this
    class stores nothing between calls and two beat processes given the same
    inputs answer identically.
    """

    def __init__(
        self,
        schedule: TradingDaySchedule,
        calendar: TradingCalendar,
        *,
        nowfun: Any = None,
        app: Any = None,
    ) -> None:
        """Bind the beat schedule to a policy and a calendar."""
        super().__init__(nowfun=nowfun, app=app)
        self._schedule = schedule
        self._calendar = calendar

    def __repr__(self) -> str:
        """Return the schedule as an operator reading a beat listing would want it."""
        return f"<trading-day: {self._schedule.describe()}>"

    def __reduce__(self) -> tuple[Any, ...]:
        """Support the pickling beat does when it persists its schedule file."""
        return (self.__class__, (self._schedule, self._calendar))

    def __eq__(self, other: object) -> bool:
        """Compare by policy alone. The calendar is a collaborator, not identity.

        Beat compares entries to decide whether a schedule has changed since the
        last run; without this, every restart would look like a change and would
        reset every job's ``last_run_at``.

        The calendar is deliberately excluded. Beat persists its entries to a
        shelve between runs, so the calendar on the other side of that file is a
        *different object* however identical it is -- and an equality that
        included it would report "changed" on every single restart, which is
        precisely the failure this method exists to prevent. It was written
        comparing calendars by identity, and the pickle round-trip test caught it.

        What beat is asking is whether the *declaration* changed, and the
        declaration is the policy. Which calendar resolves it is the composition
        root's business, and there is one per process.
        """
        if not isinstance(other, TradingDayBeatSchedule):
            return NotImplemented
        return self._schedule == other._schedule

    def __hash__(self) -> int:
        """Hash by policy, consistently with :meth:`__eq__`."""
        return hash(self._schedule)

    def is_due(self, last_run_at: datetime) -> schedstate:
        """Return whether the job is due now, and how long to sleep otherwise.

        Parameters
        ----------
        last_run_at
            When beat last ran this entry. Must be timezone-aware: a naive
            instant has no defined position in the sequence of events (ADR-006),
            and comparing one against a session boundary would be comparing a
            number to a time.

        Notes
        -----
        The next firing is sought strictly after ``last_run_at`` rather than
        after *now*, which is what makes a beat that was down over a firing catch
        it on restart instead of skipping to tomorrow.
        """
        if last_run_at.tzinfo is None or last_run_at.utcoffset() is None:
            raise ValidationError(
                "beat supplied a naive last_run_at; a schedule cannot be resolved "
                "against an instant with no timezone (ADR-006)",
                last_run_at=last_run_at.isoformat(),
            )

        now = self._now()
        due_at = self._schedule.next_after(last_run_at, self._span(now), self._calendar)

        if due_at is None:
            return schedstate(is_due=False, next=IDLE_SLEEP.total_seconds())
        if due_at <= now:
            return schedstate(is_due=True, next=self._sleep_until_after(now))
        return schedstate(is_due=False, next=self._capped(due_at - now))

    def _now(self) -> datetime:
        """Return the current instant as timezone-aware UTC."""
        current: datetime = self.now()
        return current if current.tzinfo is not None else current.replace(tzinfo=UTC)

    def _span(self, now: datetime) -> DateRange:
        """Return the date range to look for firings in.

        Starts a day *before* today. A firing anchored to yesterday's close plus
        an overnight offset lands today, and a span beginning today would select
        sessions by date and miss the session that produced it.
        """
        today = now.date()
        return DateRange(today - timedelta(days=1), today + SEARCH_HORIZON)

    def _sleep_until_after(self, now: datetime) -> float:
        """Return the sleep to report alongside a due verdict.

        Beat wants a number even when the answer is "run it now", and the honest
        one is the wait until the *following* firing -- capped, like every other.
        """
        following = self._schedule.next_after(now, self._span(now), self._calendar)
        if following is None:
            return IDLE_SLEEP.total_seconds()
        return self._capped(following - now)

    @staticmethod
    def _capped(remaining: timedelta) -> float:
        """Return seconds to sleep, never longer than :data:`MAX_SLEEP`.

        Never negative either. A firing in the past reached here through a clock
        that moved backwards, and reporting a negative sleep would make beat spin.
        """
        return max(0.0, min(remaining, MAX_SLEEP).total_seconds())
