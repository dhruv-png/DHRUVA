"""TradingDay, the calendar port, and sessions (ADR-046)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from dhruva.shared.errors import InvariantViolation
from dhruva.shared.time import (
    DateRange,
    SessionKind,
    TradingCalendar,
    TradingDay,
    TradingSession,
)
from tests.unit.shared.time.conftest import WeekdayCalendar

pytestmark = pytest.mark.unit

TUESDAY = date(2026, 7, 28)
SUNDAY = date(2026, 7, 26)


def test_the_test_calendar_satisfies_the_port(calendar: WeekdayCalendar) -> None:
    """Structural conformance, so S08's implementation cannot drift either."""
    assert isinstance(calendar, TradingCalendar)


def test_a_trading_day_must_be_verified_against_a_calendar(
    calendar: WeekdayCalendar,
) -> None:
    """The whole point of ADR-046."""
    assert TradingDay.of(TUESDAY, calendar).on == TUESDAY


def test_a_non_session_date_is_refused(calendar: WeekdayCalendar) -> None:
    """A 'trading day' that was a Sunday would propagate silently into a backtest."""
    with pytest.raises(InvariantViolation, match="not a trading session"):
        TradingDay.of(SUNDAY, calendar)


def test_a_holiday_is_refused(calendar_with_holiday: WeekdayCalendar) -> None:
    """Weekday-ness is not session-ness. Only the calendar knows."""
    with pytest.raises(InvariantViolation, match="not a trading session"):
        TradingDay.of(date(2026, 8, 14), calendar_with_holiday)


def test_session_arithmetic_skips_non_sessions(calendar: WeekdayCalendar) -> None:
    """Friday plus one session is Monday, not Saturday.

    ``day.on + timedelta(days=1)`` would give Saturday. That difference is why
    ADR-046 forbids timedelta arithmetic on trading days.
    """
    friday = TradingDay.of(date(2026, 7, 31), calendar)

    assert calendar.next_session(friday).on == date(2026, 8, 3)
    assert friday.on + timedelta(days=1) == date(2026, 8, 1)


def test_session_arithmetic_runs_backwards(calendar: WeekdayCalendar) -> None:
    """Monday minus one session is the previous Friday."""
    monday = TradingDay.of(date(2026, 8, 3), calendar)

    assert calendar.previous_session(monday).on == date(2026, 7, 31)


@pytest.mark.parametrize("count", [1, 2, 5, 10, 21])
def test_adding_and_subtracting_sessions_round_trips(calendar: WeekdayCalendar, count: int) -> None:
    """Moving n sessions forward then n back returns to the start."""
    start = TradingDay.of(TUESDAY, calendar)
    moved = calendar.add_sessions(start, count)

    assert calendar.add_sessions(moved, -count) == start


def test_sessions_between_excludes_the_end(calendar: WeekdayCalendar) -> None:
    """Half-open, like every other range in the platform."""
    span = DateRange(date(2026, 7, 27), date(2026, 8, 1))
    sessions = calendar.sessions_between(span)

    assert [d.on for d in sessions] == [
        date(2026, 7, 27),
        date(2026, 7, 28),
        date(2026, 7, 29),
        date(2026, 7, 30),
        date(2026, 7, 31),
    ]


def test_trading_days_order_chronologically(calendar: WeekdayCalendar) -> None:
    """Used for sorting and bucketing throughout the platform."""
    earlier = TradingDay.of(date(2026, 7, 27), calendar)
    later = TradingDay.of(TUESDAY, calendar)

    same = TradingDay.of(TUESDAY, calendar)

    assert earlier < later
    assert later > earlier
    assert later <= same
    assert later >= same
    assert sorted([later, earlier]) == [earlier, later]


def test_trading_days_are_hashable_and_usable_as_keys(
    calendar: WeekdayCalendar,
) -> None:
    """Bar series and daily aggregates are keyed by trading day everywhere."""
    day = TradingDay.of(TUESDAY, calendar)

    assert {day: "value"}[TradingDay.of(TUESDAY, calendar)] == "value"


def test_ordering_against_a_foreign_type_raises(calendar: WeekdayCalendar) -> None:
    """A trading day is not a date, and comparing them should say so."""
    day = TradingDay.of(TUESDAY, calendar)

    with pytest.raises(TypeError):
        _ = day < TUESDAY  # type: ignore[operator]


def test_a_trading_day_is_immutable(calendar: WeekdayCalendar) -> None:
    """Value-object semantics."""
    day = TradingDay.of(TUESDAY, calendar)

    with pytest.raises((AttributeError, TypeError)):
        day.on = SUNDAY  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #


def test_a_session_is_stored_as_utc_instants(calendar: WeekdayCalendar) -> None:
    """09:15 IST is 03:45 UTC.

    Storing UTC rather than wall-clock time means the schema does not assume
    India never adopts daylight saving. A future exchange that observes DST
    needs no reinterpretation of stored sessions.
    """
    session = calendar.session(TradingDay.of(TUESDAY, calendar))

    assert session.opens_at == datetime(2026, 7, 28, 3, 45, tzinfo=UTC)
    assert session.closes_at == datetime(2026, 7, 28, 10, 0, tzinfo=UTC)


def test_session_membership_is_half_open(calendar: WeekdayCalendar) -> None:
    """The close instant belongs to no session, so consecutive days do not overlap."""
    session = calendar.session(TradingDay.of(TUESDAY, calendar))

    assert session.contains(session.opens_at)
    assert not session.contains(session.closes_at)
    assert session.contains(session.closes_at - timedelta(microseconds=1))


def test_a_regular_session_is_not_partial(calendar: WeekdayCalendar) -> None:
    """Partial-ness is declared by the exchange, not inferred from duration."""
    assert not calendar.session(TradingDay.of(TUESDAY, calendar)).is_partial


def test_a_muhurat_session_is_partial(muhurat_session: TradingSession) -> None:
    """The ceremonial Diwali session, which breaks fixed-hours assumptions."""
    assert muhurat_session.is_partial
    assert muhurat_session.kind is SessionKind.MUHURAT


def test_a_session_must_close_after_it_opens() -> None:
    """An inverted session would make every duration negative."""
    day = TradingDay(TUESDAY)

    with pytest.raises(InvariantViolation, match="close after it opens"):
        TradingSession(
            day=day,
            opens_at=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
            closes_at=datetime(2026, 7, 28, 3, 45, tzinfo=UTC),
        )


def test_a_session_rejects_naive_instants() -> None:
    """ADR-006, enforced at the boundary rather than hoped for."""
    with pytest.raises(InvariantViolation, match="timezone-aware"):
        TradingSession(
            day=TradingDay(TUESDAY),
            opens_at=datetime(2026, 7, 28, 3, 45),  # noqa: DTZ001 - asserting rejection
            closes_at=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
        )
