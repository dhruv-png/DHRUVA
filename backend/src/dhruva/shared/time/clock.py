"""The time source, as an injected port (ADR-011).

Nothing in this platform calls :func:`datetime.datetime.now`. Time arrives
through a :class:`Clock`, for two reasons that both matter more than the small
indirection costs:

* **Determinism.** ADR-010 requires strategy code to run unchanged in backtest,
  paper and live. That is only possible if the strategy cannot tell which one it
  is in, and the wall clock is the most obvious way it could tell.
* **Testability.** A test that depends on the current instant is a test that
  fails on the first day of a month, or during a leap second, or when CI is slow.

All clocks return timezone-aware UTC (ADR-006). A naive datetime never enters the
system, and :class:`FrozenClock` rejects one at construction rather than letting
it propagate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from dhruva.shared.invariants import invariant

__all__ = ["Clock", "FrozenClock", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    """A source of the current instant.

    Injected wherever time is needed. Implementations must return timezone-aware
    UTC; a naive datetime is a defect, not a variation.
    """

    def now(self) -> datetime:
        """Return the current instant as timezone-aware UTC."""
        ...


class SystemClock:
    """The real clock. The only place the wall clock is read.

    Used by composition roots in every environment including backtests -- a
    backtest reads the *simulated* clock for market time, but still uses the
    system clock for its own wall-time measurements such as elapsed runtime.
    """

    __slots__ = ()

    def now(self) -> datetime:
        """Return the current instant as timezone-aware UTC."""
        return datetime.now(UTC)

    def __repr__(self) -> str:
        """Return an unambiguous representation."""
        return "SystemClock()"


class FrozenClock:
    """A clock that does not move unless told to.

    The backtest clock and the test clock are the same object, which is the
    point: if a strategy behaves differently under a frozen clock than under a
    real one, that difference is a bug the shared execution kernel (S26) exists
    to prevent.

    Examples
    --------
    >>> from datetime import datetime, UTC
    >>> clock = FrozenClock(datetime(2026, 7, 26, 9, 15, tzinfo=UTC))
    >>> clock.now().hour
    9
    >>> clock.advance(minutes=15)
    >>> clock.now().hour, clock.now().minute
    (9, 30)
    """

    __slots__ = ("_instant",)

    _instant: datetime

    def __init__(self, instant: datetime, /) -> None:
        """Freeze at ``instant``.

        Raises
        ------
        InvariantViolation
            If ``instant`` is naive. A naive datetime has no defined moment, and
            accepting one here would let ambiguity into every test that uses it.
        """
        _require_aware(instant, "FrozenClock instant")
        self._instant = instant.astimezone(UTC)

    def now(self) -> datetime:
        """Return the frozen instant as timezone-aware UTC."""
        return self._instant

    def advance(
        self, *, days: int = 0, hours: int = 0, minutes: int = 0, seconds: float = 0
    ) -> None:
        """Move the clock forward.

        Deliberately forward-only. A clock that can move backwards can produce an
        event ordering that could not occur in reality, and every downstream
        assumption about monotonic time would silently hold anyway -- until it
        did not.

        Raises
        ------
        InvariantViolation
            If the requested movement is not strictly forward.
        """
        delta = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
        invariant(
            delta > timedelta(0),
            "a clock may only advance forward",
            requested_seconds=delta.total_seconds(),
        )
        self._instant = self._instant + delta

    def set_to(self, instant: datetime, /) -> None:
        """Jump to a specific instant.

        Permitted where :meth:`advance` is not, because setting up a scenario is
        a different act from simulating the passage of time. Tests use this to
        arrange; they use :meth:`advance` to act.
        """
        _require_aware(instant, "FrozenClock instant")
        self._instant = instant.astimezone(UTC)

    def __repr__(self) -> str:
        """Return an unambiguous representation."""
        return f"FrozenClock({self._instant.isoformat()})"


def _require_aware(instant: datetime, what: str) -> None:
    """Reject a naive datetime (ADR-006)."""
    invariant(
        instant.tzinfo is not None and instant.utcoffset() is not None,
        f"{what} must be timezone-aware; storage and logic are UTC",
        value=instant.isoformat(),
    )
