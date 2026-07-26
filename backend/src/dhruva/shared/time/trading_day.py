"""Trading days and the calendar that defines them (ADR-046).

A ``TradingDay`` that cannot verify it is a trading day is a lie the type tells.
So it cannot be constructed without a calendar, and calendar arithmetic never
uses ``timedelta``.

This matters more than it looks. NSE moved derivative expiry from Thursday to
Tuesday on 2025-09-01, and BSE moved to Thursday. Any backtest spanning that date
which treats "the next expiry" as calendar arithmetic is wrong, and nothing in
the data will flag it. Routing every session question through the calendar is
what makes the answer correct on both sides of the change.

The calendar itself lives in the Reference context (S08). The shared kernel owns
only the port, so that the dependency direction stays right: everything may
depend on the *idea* of a calendar; only S08 knows the holidays.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol, Self, runtime_checkable

from dhruva.shared.invariants import invariant

__all__ = ["SessionKind", "TradingCalendar", "TradingDay", "TradingSession"]


class SessionKind(StrEnum):
    """What sort of session a trading day carries.

    Attributes
    ----------
    REGULAR
        An ordinary full session.
    MUHURAT
        The ceremonial Diwali session. Short, and its own trading day for
        settlement purposes -- which is exactly the kind of edge case that
        breaks code assuming one session per date with fixed hours.
    SPECIAL
        An exchange-declared session outside the normal schedule: a live trading
        session on a Saturday for disaster-recovery testing, or a shortened day.
    """

    REGULAR = "regular"
    MUHURAT = "muhurat"
    SPECIAL = "special"


@dataclass(frozen=True, slots=True)
class TradingDay:
    """A calendar date on which a market was open.

    Constructed only through a :class:`TradingCalendar`. The bare constructor is
    reachable, because Python, but :meth:`of` is the sanctioned route and calendar
    implementations are the only other legitimate caller.

    Ordering and hashing are by date, so a trading day works as a dictionary key
    and sorts naturally -- both of which the platform relies on heavily.
    """

    on: date

    @classmethod
    def of(cls, day: date, calendar: TradingCalendar) -> Self:
        """Build a trading day, verifying it against ``calendar``.

        Raises
        ------
        InvariantViolation
            If ``day`` is not a session on that calendar. Refusing here is the
            entire purpose of the type: a "trading day" that was a Sunday would
            otherwise propagate silently into a backtest.
        """
        invariant(
            calendar.is_session(day),
            "date is not a trading session on this calendar",
            day=day.isoformat(),
            calendar=type(calendar).__name__,
        )
        return cls(day)

    @property
    def iso(self) -> str:
        """Return the ISO-8601 date, which is how it is persisted and logged."""
        return self.on.isoformat()

    def __lt__(self, other: TradingDay) -> bool:
        """Order chronologically."""
        if not isinstance(other, TradingDay):
            return NotImplemented
        return self.on < other.on

    def __le__(self, other: TradingDay) -> bool:
        """Order chronologically."""
        if not isinstance(other, TradingDay):
            return NotImplemented
        return self.on <= other.on

    def __gt__(self, other: TradingDay) -> bool:
        """Order chronologically."""
        if not isinstance(other, TradingDay):
            return NotImplemented
        return self.on > other.on

    def __ge__(self, other: TradingDay) -> bool:
        """Order chronologically."""
        if not isinstance(other, TradingDay):
            return NotImplemented
        return self.on >= other.on

    def __str__(self) -> str:
        """Return the ISO date."""
        return self.iso


@dataclass(frozen=True, slots=True)
class TradingSession:
    """The open and close of one trading day, as instants.

    Stored in UTC (ADR-006) rather than as local wall-clock times. India does not
    observe daylight saving, so this costs nothing today -- but storing wall-clock
    times would bake that assumption into the schema, and a future exchange that
    does observe DST would need every stored session reinterpreted. UTC instants
    have one meaning regardless.

    Attributes
    ----------
    day
        The trading day this session belongs to.
    opens_at, closes_at
        Session bounds as timezone-aware UTC instants, half-open ``[open, close)``.
    kind
        Whether this is a regular, Muhurat or exchange-declared special session.
    """

    day: TradingDay
    opens_at: datetime
    closes_at: datetime
    kind: SessionKind = SessionKind.REGULAR

    def __post_init__(self) -> None:
        """Reject naive instants and a close that does not follow its open."""
        for label, value in (("opens_at", self.opens_at), ("closes_at", self.closes_at)):
            invariant(
                value.tzinfo is not None and value.utcoffset() is not None,
                f"session {label} must be timezone-aware",
                value=value.isoformat(),
            )
        invariant(
            self.opens_at < self.closes_at,
            "a session must close after it opens",
            opens_at=self.opens_at.isoformat(),
            closes_at=self.closes_at.isoformat(),
        )

    @property
    def is_partial(self) -> bool:
        """Return whether this session is shorter than a regular one.

        Determined by kind rather than by duration. A Muhurat session is partial
        because the exchange declared it so, not because it happens to be an hour
        long, and a regular session cut short by a halt is still a regular
        session.
        """
        return self.kind is not SessionKind.REGULAR

    def contains(self, instant: datetime) -> bool:
        """Return whether ``instant`` falls within the session, half-open."""
        return self.opens_at <= instant < self.closes_at


@runtime_checkable
class TradingCalendar(Protocol):
    """The authority on which dates are sessions and when they run.

    Implemented in the Reference context (S08). Declared here so that everything
    can depend on the idea of a calendar without depending on the holidays.

    Every method that answers a "when" question about the market belongs here.
    If code elsewhere is doing arithmetic on dates to answer one, that code is
    wrong -- which is the failure the 2025-09-01 expiry change would otherwise
    have caused silently.
    """

    def is_session(self, day: date) -> bool:
        """Return whether the market was open on ``day``."""
        ...

    def session(self, day: TradingDay) -> TradingSession:
        """Return the session bounds for ``day``."""
        ...

    def next_session(self, day: TradingDay) -> TradingDay:
        """Return the next trading day after ``day``."""
        ...

    def previous_session(self, day: TradingDay) -> TradingDay:
        """Return the trading day before ``day``."""
        ...

    def add_sessions(self, day: TradingDay, count: int) -> TradingDay:
        """Return the trading day ``count`` sessions away.

        Negative counts move backwards. This is the only sanctioned way to do
        session arithmetic; ``day.on + timedelta(days=n)`` is not equivalent and
        is not correct.
        """
        ...

    def sessions_between(self, span: DateRangeLike) -> Sequence[TradingDay]:
        """Return every trading day in the half-open range ``span``."""
        ...


class DateRangeLike(Protocol):
    """The half-open date range shape :meth:`TradingCalendar.sessions_between` accepts.

    Declared structurally rather than importing
    :class:`~dhruva.shared.time.ranges.DateRange` directly, so the calendar port
    does not force a concrete range type on implementations.
    """

    @property
    def start(self) -> date:
        """Return the inclusive start."""
        ...

    @property
    def end(self) -> date:
        """Return the exclusive end."""
        ...
