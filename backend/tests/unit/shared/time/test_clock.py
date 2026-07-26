"""The Clock port and its two implementations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from dhruva.shared.errors import InvariantViolation
from dhruva.shared.time import Clock, FrozenClock, SystemClock

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("clock", [SystemClock(), FrozenClock(datetime.now(UTC))])
def test_every_clock_satisfies_the_port(clock: Clock) -> None:
    """Structural conformance, so an implementation cannot drift from the port."""
    assert isinstance(clock, Clock)


def test_the_system_clock_returns_aware_utc() -> None:
    """Naive time never enters the platform (ADR-006)."""
    now = SystemClock().now()

    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_a_frozen_clock_does_not_move_on_its_own() -> None:
    """The property that makes backtests and tests deterministic (ADR-011)."""
    instant = datetime(2026, 7, 28, 3, 45, tzinfo=UTC)
    clock = FrozenClock(instant)

    assert clock.now() == clock.now() == instant


def test_advancing_moves_the_clock_forward() -> None:
    """Simulated time passes only when the simulation says so."""
    clock = FrozenClock(datetime(2026, 7, 28, 3, 45, tzinfo=UTC))

    clock.advance(minutes=15)

    assert clock.now() == datetime(2026, 7, 28, 4, 0, tzinfo=UTC)


@pytest.mark.parametrize("kwargs", [{"minutes": 0}, {"minutes": -1}, {"days": -1}, {"seconds": 0}])
def test_a_clock_cannot_move_backwards(kwargs: dict[str, int]) -> None:
    """A clock that can run backwards can produce an impossible event ordering.

    Every downstream assumption about monotonic time would keep holding anyway --
    until it did not, in a way no test would have caught.
    """
    clock = FrozenClock(datetime(2026, 7, 28, tzinfo=UTC))

    with pytest.raises(InvariantViolation, match="only advance forward"):
        clock.advance(**kwargs)


def test_setting_the_clock_is_permitted_where_advancing_is_not() -> None:
    """Arranging a scenario is a different act from simulating elapsed time."""
    clock = FrozenClock(datetime(2026, 7, 28, tzinfo=UTC))

    clock.set_to(datetime(2020, 1, 1, tzinfo=UTC))

    assert clock.now().year == 2020


def test_a_naive_instant_is_refused() -> None:
    """A naive datetime has no defined moment; accepting one spreads ambiguity."""
    with pytest.raises(InvariantViolation, match="timezone-aware"):
        FrozenClock(datetime(2026, 7, 28))  # noqa: DTZ001 - asserting the rejection


def test_a_non_utc_instant_is_normalised_rather_than_rejected() -> None:
    """Callers may hold local time; storage is always UTC (ADR-006)."""
    ist = timezone(timedelta(hours=5, minutes=30))
    clock = FrozenClock(datetime(2026, 7, 28, 9, 15, tzinfo=ist))

    assert clock.now() == datetime(2026, 7, 28, 3, 45, tzinfo=UTC)
    assert clock.now().utcoffset() == timedelta(0)


def test_repr_identifies_the_instant() -> None:
    """A test failing on a clock should say which instant it was frozen at."""
    assert "2026-07-28" in repr(FrozenClock(datetime(2026, 7, 28, tzinfo=UTC)))
    assert repr(SystemClock()) == "SystemClock()"
