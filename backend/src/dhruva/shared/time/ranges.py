"""Half-open intervals.

Every range in this platform is ``[start, end)`` — inclusive of its start,
exclusive of its end. The convention is chosen once and applied everywhere, for
a specific reason: half-open ranges tile without gaps and without overlap.
Consecutive trading days, consecutive candle intervals, and consecutive
backtest windows can be laid end to end and every instant belongs to exactly one.

Closed ranges cannot do this. ``[09:15, 15:30]`` followed by ``[15:30, 15:45]``
contains 15:30 twice, which is how a bar gets counted in two windows and a
backtest quietly reports more trades than occurred.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from dhruva.shared.invariants import invariant

__all__ = ["DateRange", "TimeRange"]


@dataclass(frozen=True, slots=True)
class DateRange:
    """A half-open range of calendar dates, ``[start, end)``.

    Note that this is *calendar* dates. For a span of trading sessions, ask the
    calendar (ADR-046) — the number of sessions between two dates is not a
    property of the dates.

    Examples
    --------
    >>> from datetime import date
    >>> span = DateRange(date(2026, 1, 1), date(2026, 1, 4))
    >>> len(list(span))
    3
    >>> date(2026, 1, 4) in span
    False
    """

    start: date
    end: date

    def __post_init__(self) -> None:
        """Reject an inverted range.

        Not normalised by swapping. A caller passing them the wrong way round has
        a bug, and silently correcting it would hide the bug while producing a
        range they did not ask for.
        """
        invariant(
            self.start <= self.end,
            "range start must not be after its end",
            start=self.start.isoformat(),
            end=self.end.isoformat(),
        )

    @property
    def is_empty(self) -> bool:
        """Return whether the range contains no dates."""
        return self.start == self.end

    @property
    def days(self) -> int:
        """Return the number of calendar dates in the range."""
        return (self.end - self.start).days

    def __contains__(self, value: date) -> bool:
        """Return whether ``value`` falls inside, honouring the exclusive end."""
        return self.start <= value < self.end

    def __iter__(self) -> Iterator[date]:
        """Yield each date in the range, ascending."""
        current = self.start
        while current < self.end:
            yield current
            current += timedelta(days=1)

    def overlaps(self, other: DateRange) -> bool:
        """Return whether the two ranges share at least one date."""
        return self.start < other.end and other.start < self.end

    def intersection(self, other: DateRange) -> DateRange:
        """Return the overlapping portion, which may be empty."""
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        return DateRange(start, end) if start < end else DateRange(start, start)

    def __str__(self) -> str:
        """Render in the half-open notation, so the convention is visible."""
        return f"[{self.start.isoformat()}, {self.end.isoformat()})"


@dataclass(frozen=True, slots=True)
class TimeRange:
    """A half-open range of instants, ``[start, end)``.

    Both bounds must be timezone-aware (ADR-006). Comparing a naive instant to an
    aware one raises in Python, which is the correct behaviour and a poor place
    to discover it.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        """Reject naive bounds and inverted ranges."""
        for label, value in (("start", self.start), ("end", self.end)):
            invariant(
                value.tzinfo is not None and value.utcoffset() is not None,
                f"TimeRange {label} must be timezone-aware",
                value=value.isoformat(),
            )
        invariant(
            self.start <= self.end,
            "range start must not be after its end",
            start=self.start.isoformat(),
            end=self.end.isoformat(),
        )

    @property
    def is_empty(self) -> bool:
        """Return whether the range contains no instants."""
        return self.start == self.end

    @property
    def duration(self) -> timedelta:
        """Return the length of the range."""
        return self.end - self.start

    def __contains__(self, value: datetime) -> bool:
        """Return whether ``value`` falls inside, honouring the exclusive end."""
        return self.start <= value < self.end

    def overlaps(self, other: TimeRange) -> bool:
        """Return whether the two ranges share at least one instant."""
        return self.start < other.end and other.start < self.end

    def intersection(self, other: TimeRange) -> TimeRange:
        """Return the overlapping portion, which may be empty."""
        start = max(self.start, other.start)
        end = min(self.end, other.end)
        return TimeRange(start, end) if start < end else TimeRange(start, start)

    def __str__(self) -> str:
        """Render in the half-open notation, so the convention is visible."""
        return f"[{self.start.isoformat()}, {self.end.isoformat()})"
