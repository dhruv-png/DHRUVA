"""Calendar-aware scheduling (ADR-066).

The point of ADR-066 is that a schedule can be asserted against a holiday
without waiting for one, so the calendar here is a stub built from a real
fortnight of the NSE calendar: a weekend, a declared holiday, a half day, and
the Muhurat session. Each of those broke something in some real system once, and
a stub that contained only ordinary Tuesdays would prove nothing.

Nothing here sleeps and nothing reads a clock. Every instant is either supplied
or derived from one that was.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dhruva.contexts.platform.domain.scheduling import (
    MAX_OFFSET,
    SessionAnchor,
    TradingDaySchedule,
)
from dhruva.shared.errors import ValidationError
from dhruva.shared.time import (
    DateRange,
    SessionKind,
    TradingDay,
    TradingSession,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dhruva.shared.time import DateRangeLike

pytestmark = pytest.mark.unit

#: IST is UTC+5:30 and India observes no daylight saving, so a 09:15 open is
#: 03:45 UTC on every date in the corpus. Written as UTC because that is what the
#: port stores (ADR-006); the IST equivalent is in the comment so a reader can
#: check the arithmetic without converting in their head.
OPEN_UTC = timedelta(hours=3, minutes=45)  # 09:15 IST
CLOSE_UTC = timedelta(hours=10)  # 15:30 IST
HALF_DAY_CLOSE_UTC = timedelta(hours=7)  # 12:30 IST
MUHURAT_OPEN_UTC = timedelta(hours=12, minutes=45)  # 18:15 IST
MUHURAT_CLOSE_UTC = timedelta(hours=13, minutes=45)  # 19:15 IST


def instant(day: date, offset: timedelta) -> datetime:
    """Return a UTC instant on ``day``."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC) + offset


#: A real fortnight, chosen for what it contains rather than for convenience.
#:
#: 2026-11-06 is a Friday; 07 and 08 are the weekend. 2026-11-09 stands in for a
#: declared exchange holiday -- a Monday, because a holiday that fell on a
#: weekend would be indistinguishable from the weekend and would test nothing.
#: 2026-11-10 is a shortened session, and 2026-11-11 carries the Muhurat evening
#: session, which is its own trading day.
SESSIONS: dict[date, tuple[timedelta, timedelta, SessionKind]] = {
    date(2026, 11, 5): (OPEN_UTC, CLOSE_UTC, SessionKind.REGULAR),
    date(2026, 11, 6): (OPEN_UTC, CLOSE_UTC, SessionKind.REGULAR),
    # 7th and 8th: weekend, absent.
    # 9th: exchange holiday, absent.
    date(2026, 11, 10): (OPEN_UTC, HALF_DAY_CLOSE_UTC, SessionKind.SPECIAL),
    date(2026, 11, 11): (MUHURAT_OPEN_UTC, MUHURAT_CLOSE_UTC, SessionKind.MUHURAT),
    date(2026, 11, 12): (OPEN_UTC, CLOSE_UTC, SessionKind.REGULAR),
    date(2026, 11, 13): (OPEN_UTC, CLOSE_UTC, SessionKind.REGULAR),
}


class StubCalendar:
    """A calendar over :data:`SESSIONS`. Satisfies the ``TradingCalendar`` port."""

    def is_session(self, day: date) -> bool:
        """Return whether the exchange was open on ``day``."""
        return day in SESSIONS

    def session(self, day: TradingDay) -> TradingSession:
        """Return the session for a trading day."""
        opens, closes, kind = SESSIONS[day.on]
        return TradingSession(
            day=day,
            opens_at=instant(day.on, opens),
            closes_at=instant(day.on, closes),
            kind=kind,
        )

    def next_session(self, day: TradingDay) -> TradingDay:
        """Return the following trading day."""
        later = [d for d in sorted(SESSIONS) if d > day.on]
        return TradingDay(later[0])

    def previous_session(self, day: TradingDay) -> TradingDay:
        """Return the preceding trading day."""
        earlier = [d for d in sorted(SESSIONS) if d < day.on]
        return TradingDay(earlier[-1])

    def add_sessions(self, day: TradingDay, count: int) -> TradingDay:
        """Return the trading day ``count`` sessions away."""
        ordered = sorted(SESSIONS)
        return TradingDay(ordered[ordered.index(day.on) + count])

    def sessions_between(self, span: DateRangeLike) -> Sequence[TradingDay]:
        """Return the trading days inside a half-open date range."""
        return [TradingDay(d) for d in sorted(SESSIONS) if span.start <= d < span.end]


CALENDAR = StubCalendar()
FORTNIGHT = DateRange(date(2026, 11, 1), date(2026, 11, 15))


def session_on(day: date) -> TradingSession:
    """Return the stub session for a date known to be a session."""
    return CALENDAR.session(TradingDay(day))


# --------------------------------------------------------------------------- #
# The arithmetic, without a calendar
# --------------------------------------------------------------------------- #


def test_a_schedule_with_no_offset_fires_at_the_anchor() -> None:
    """The base case, so the offsets below are measured from something known."""
    session = session_on(date(2026, 11, 5))

    at_open = TradingDaySchedule(anchor=SessionAnchor.MARKET_OPEN).at(session)
    at_close = TradingDaySchedule(anchor=SessionAnchor.MARKET_CLOSE).at(session)

    assert at_open == session.opens_at
    assert at_close == session.closes_at


@pytest.mark.parametrize(
    ("anchor", "offset", "expected"),
    [
        (SessionAnchor.MARKET_OPEN, timedelta(minutes=5), timedelta(hours=3, minutes=50)),
        (SessionAnchor.MARKET_OPEN, -timedelta(minutes=30), timedelta(hours=3, minutes=15)),
        (SessionAnchor.MARKET_CLOSE, -timedelta(minutes=15), timedelta(hours=9, minutes=45)),
        (SessionAnchor.MARKET_CLOSE, timedelta(hours=1), timedelta(hours=11)),
    ],
)
def test_the_offset_is_applied_in_the_direction_it_reads(
    anchor: SessionAnchor, offset: timedelta, expected: timedelta
) -> None:
    """ADR-066's own examples, pinned. A sign error here is silent and daily."""
    fires = TradingDaySchedule(anchor=anchor, offset=offset).at(session_on(date(2026, 11, 5)))

    assert fires == instant(date(2026, 11, 5), expected)


def test_a_pre_open_schedule_fires_before_the_market_opens() -> None:
    """A negative offset against the open is warm-up work, not an error."""
    session = session_on(date(2026, 11, 5))

    fires = TradingDaySchedule(anchor=SessionAnchor.MARKET_OPEN, offset=-timedelta(minutes=30)).at(
        session
    )

    assert fires is not None
    assert fires < session.opens_at
    assert not session.contains(fires), "pre-open work happens outside the session"


def test_an_offset_may_carry_a_firing_past_midnight() -> None:
    """A post-close job on a late session lands on the next calendar date.

    Deliberately permitted. The moment is still anchored to the session it names,
    and refusing it would push overnight reconciliation back into cron -- where
    it would once again be wrong about holidays.
    """
    session = session_on(date(2026, 11, 11))  # Muhurat, closes 19:15 IST

    fires = TradingDaySchedule(anchor=SessionAnchor.MARKET_CLOSE, offset=timedelta(hours=12)).at(
        session
    )

    assert fires == instant(date(2026, 11, 12), timedelta(hours=1, minutes=45))


def test_the_anchor_follows_a_shortened_session_rather_than_the_clock() -> None:
    """The whole reason cron is wrong.

    A half day closes at 12:30 IST, and "close - 15min" must move with it. A cron
    expression saying 15:15 would fire two and three quarter hours after the
    market had shut, on a day when it mattered.
    """
    half_day = session_on(date(2026, 11, 10))
    regular = session_on(date(2026, 11, 5))
    schedule = TradingDaySchedule(anchor=SessionAnchor.MARKET_CLOSE, offset=-timedelta(minutes=15))

    assert schedule.at(half_day) == instant(date(2026, 11, 10), timedelta(hours=6, minutes=45))
    assert schedule.at(regular) == instant(date(2026, 11, 5), timedelta(hours=9, minutes=45))


# --------------------------------------------------------------------------- #
# Which sessions qualify
# --------------------------------------------------------------------------- #


def test_every_session_kind_qualifies_by_default() -> None:
    """Forgetting Muhurat means a settlement job skipping the session everybody remembers."""
    schedule = TradingDaySchedule(anchor=SessionAnchor.MARKET_OPEN)

    assert schedule.at(session_on(date(2026, 11, 11))) is not None
    assert schedule.at(session_on(date(2026, 11, 10))) is not None


def test_a_schedule_restricted_to_regular_sessions_skips_the_others() -> None:
    """A moment that does not occur today simply does not run (ADR-066).

    Not an error, and not a failed schedule -- which is why it returns ``None``
    rather than raising. A raise here would fill an operator's alerts with
    holidays.
    """
    schedule = TradingDaySchedule(
        anchor=SessionAnchor.MARKET_OPEN, kinds=frozenset({SessionKind.REGULAR})
    )

    assert schedule.at(session_on(date(2026, 11, 5))) is not None
    assert schedule.at(session_on(date(2026, 11, 11))) is None
    assert schedule.at(session_on(date(2026, 11, 10))) is None


def test_a_schedule_may_target_only_the_muhurat_session() -> None:
    """The ceremonial session has jobs nothing else does."""
    schedule = TradingDaySchedule(
        anchor=SessionAnchor.MARKET_OPEN, kinds=frozenset({SessionKind.MUHURAT})
    )

    assert schedule.at(session_on(date(2026, 11, 11))) is not None
    assert schedule.at(session_on(date(2026, 11, 5))) is None


# --------------------------------------------------------------------------- #
# Across a calendar
# --------------------------------------------------------------------------- #


def test_a_holiday_and_a_weekend_produce_no_occurrence() -> None:
    """The assertion ADR-066 exists to make possible, made without waiting for Diwali."""
    schedule = TradingDaySchedule(anchor=SessionAnchor.MARKET_OPEN)

    fired_on = {fire.date() for fire in schedule.occurrences(FORTNIGHT, CALENDAR)}

    assert date(2026, 11, 7) not in fired_on, "Saturday"
    assert date(2026, 11, 8) not in fired_on, "Sunday"
    assert date(2026, 11, 9) not in fired_on, "declared exchange holiday"
    assert fired_on == set(SESSIONS)


def test_occurrences_are_ordered() -> None:
    """A caller planning a day's work relies on it, so it is asserted rather than trusted."""
    fires = TradingDaySchedule(
        anchor=SessionAnchor.MARKET_CLOSE, offset=timedelta(minutes=45)
    ).occurrences(FORTNIGHT, CALENDAR)

    assert list(fires) == sorted(fires)
    assert len(fires) == len(SESSIONS)


def test_a_span_with_no_sessions_produces_nothing() -> None:
    """A quiet week is not an error."""
    quiet = DateRange(date(2026, 11, 7), date(2026, 11, 10))
    schedule = TradingDaySchedule(anchor=SessionAnchor.MARKET_OPEN)

    assert schedule.occurrences(quiet, CALENDAR) == ()


def test_an_occurrence_after_the_final_session_is_still_included() -> None:
    """The span selects sessions by date, not firings by instant.

    A post-close job on the last session in the span fires on the following
    calendar date. Filtering by firing instant would silently drop it, and the
    job would never run on the last day of any window.
    """
    last_day_only = DateRange(date(2026, 11, 13), date(2026, 11, 14))
    schedule = TradingDaySchedule(anchor=SessionAnchor.MARKET_CLOSE, offset=timedelta(hours=20))

    [fire] = schedule.occurrences(last_day_only, CALENDAR)

    assert fire.date() == date(2026, 11, 14)
    assert fire.date() not in SESSIONS


# --------------------------------------------------------------------------- #
# What a beat asks
# --------------------------------------------------------------------------- #


def test_the_next_firing_skips_a_holiday() -> None:
    """From Friday afternoon, the next open is Tuesday -- not Monday, and not Saturday."""
    schedule = TradingDaySchedule(anchor=SessionAnchor.MARKET_OPEN)
    friday_afternoon = instant(date(2026, 11, 6), timedelta(hours=11))

    following = schedule.next_after(friday_afternoon, FORTNIGHT, CALENDAR)

    assert following == instant(date(2026, 11, 10), OPEN_UTC)


def test_the_next_firing_is_strictly_after_the_instant_given() -> None:
    """A beat that has just run must not immediately find the same occurrence.

    Inclusive comparison here would run every job exactly twice, and the second
    run would look like a redelivery rather than a scheduling defect.
    """
    schedule = TradingDaySchedule(anchor=SessionAnchor.MARKET_OPEN)
    an_open = instant(date(2026, 11, 5), OPEN_UTC)

    assert schedule.next_after(an_open, FORTNIGHT, CALENDAR) == instant(date(2026, 11, 6), OPEN_UTC)


def test_no_further_firing_inside_the_span_returns_nothing() -> None:
    """Bounded by construction: an exhausted span answers, rather than searching on."""
    schedule = TradingDaySchedule(anchor=SessionAnchor.MARKET_CLOSE)
    after_everything = instant(date(2026, 11, 20), timedelta(0))

    assert schedule.next_after(after_everything, FORTNIGHT, CALENDAR) is None


def test_a_schedule_that_never_qualifies_returns_nothing_rather_than_hanging() -> None:
    """A Muhurat-only schedule asked about a March span must answer.

    An unbounded "when next?" would search forward forever, inside the scheduler,
    where nobody reads a stack trace.
    """
    march = DateRange(date(2026, 3, 1), date(2026, 3, 31))
    schedule = TradingDaySchedule(
        anchor=SessionAnchor.MARKET_OPEN, kinds=frozenset({SessionKind.MUHURAT})
    )

    assert schedule.next_after(instant(date(2026, 3, 1), timedelta(0)), march, CALENDAR) is None


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def test_a_schedule_with_no_session_kinds_is_refused() -> None:
    """It would never run, and would report nothing wrong while not running."""
    with pytest.raises(ValidationError):
        TradingDaySchedule(anchor=SessionAnchor.MARKET_OPEN, kinds=frozenset())


@pytest.mark.parametrize("offset", [MAX_OFFSET + timedelta(seconds=1), -timedelta(days=3)])
def test_an_offset_beyond_a_day_is_refused(offset: timedelta) -> None:
    """Beyond a day the moment belongs to a different session than the one named.

    "market_close + 30h" is Sunday on a Friday and Tuesday lunchtime on a Monday.
    Neither reads that way, and ADR-066 leaves cron for work genuinely detached
    from a session.
    """
    with pytest.raises(ValidationError):
        TradingDaySchedule(anchor=SessionAnchor.MARKET_CLOSE, offset=offset)


def test_an_offset_of_exactly_one_day_is_permitted() -> None:
    """The bound is inclusive; a next-morning job is still session-relative."""
    schedule = TradingDaySchedule(anchor=SessionAnchor.MARKET_CLOSE, offset=MAX_OFFSET)

    assert schedule.offset == MAX_OFFSET


def test_a_schedule_is_immutable() -> None:
    """A schedule a caller can edit is a schedule two processes disagree about."""
    schedule = TradingDaySchedule(anchor=SessionAnchor.MARKET_OPEN)

    with pytest.raises(AttributeError):
        schedule.offset = timedelta(hours=1)  # type: ignore[misc]


def test_schedules_compare_by_value() -> None:
    """Beat registries deduplicate on them, so equality must mean what it says."""
    first = TradingDaySchedule(anchor=SessionAnchor.MARKET_OPEN, offset=timedelta(minutes=5))
    second = TradingDaySchedule(anchor=SessionAnchor.MARKET_OPEN, offset=timedelta(minutes=5))

    assert first == second
    assert len({first, second}) == 1


@pytest.mark.parametrize(
    ("schedule", "rendered"),
    [
        (TradingDaySchedule(anchor=SessionAnchor.MARKET_OPEN), "market_open"),
        (
            TradingDaySchedule(anchor=SessionAnchor.MARKET_OPEN, offset=timedelta(minutes=5)),
            "market_open + 0:05:00",
        ),
        (
            TradingDaySchedule(anchor=SessionAnchor.MARKET_CLOSE, offset=-timedelta(minutes=15)),
            "market_close - 0:15:00",
        ),
    ],
)
def test_a_schedule_describes_itself_the_way_it_was_written(
    schedule: TradingDaySchedule, rendered: str
) -> None:
    """An operator reading a beat listing must not have to decode a sign."""
    assert schedule.describe() == rendered


# --------------------------------------------------------------------------- #
# Properties
# --------------------------------------------------------------------------- #

offsets = st.timedeltas(min_value=-MAX_OFFSET, max_value=MAX_OFFSET)
anchors = st.sampled_from(list(SessionAnchor))


@given(anchor=anchors, offset=offsets)
def test_a_firing_is_always_the_anchor_plus_the_offset(
    anchor: SessionAnchor, offset: timedelta
) -> None:
    """The relationship, stated once rather than per example."""
    session = session_on(date(2026, 11, 5))
    expected = session.opens_at if anchor is SessionAnchor.MARKET_OPEN else session.closes_at

    assert TradingDaySchedule(anchor=anchor, offset=offset).at(session) == expected + offset


@given(anchor=anchors, offset=offsets)
def test_occurrences_are_ordered_for_every_schedule(
    anchor: SessionAnchor, offset: timedelta
) -> None:
    """Ordering survives any offset, because sessions do not overlap.

    Worth a property rather than an example: a negative offset large enough to
    reach back past the previous session's close would break it, and the bound on
    ``MAX_OFFSET`` is what prevents that. This asserts the two rules agree.
    """
    fires = TradingDaySchedule(anchor=anchor, offset=offset).occurrences(FORTNIGHT, CALENDAR)

    assert list(fires) == sorted(fires)


@given(anchor=anchors, offset=offsets)
def test_a_firing_is_always_timezone_aware(anchor: SessionAnchor, offset: timedelta) -> None:
    """ADR-006. A naive instant has no defined position in the sequence of events."""
    fires = TradingDaySchedule(anchor=anchor, offset=offset).at(session_on(date(2026, 11, 5)))

    assert fires is not None
    assert fires.tzinfo is not None
    assert fires.utcoffset() is not None


@given(anchor=anchors, offset=offsets)
def test_the_next_firing_is_the_first_occurrence_after_the_instant(
    anchor: SessionAnchor, offset: timedelta
) -> None:
    """``next_after`` and ``occurrences`` must not be able to disagree."""
    schedule = TradingDaySchedule(anchor=anchor, offset=offset)
    fires = schedule.occurrences(FORTNIGHT, CALENDAR)
    probe = fires[1]

    following = schedule.next_after(probe, FORTNIGHT, CALENDAR)

    assert following == fires[2]


@given(
    anchor=anchors,
    offset=offsets,
    kinds=st.sets(st.sampled_from(list(SessionKind)), min_size=1),
)
def test_restricting_kinds_never_adds_an_occurrence(
    anchor: SessionAnchor, offset: timedelta, kinds: set[SessionKind]
) -> None:
    """A filter subsets. Anything else would mean a restriction that scheduled *more*."""
    everything = TradingDaySchedule(anchor=anchor, offset=offset)
    restricted = TradingDaySchedule(anchor=anchor, offset=offset, kinds=frozenset(kinds))

    assert set(restricted.occurrences(FORTNIGHT, CALENDAR)) <= set(
        everything.occurrences(FORTNIGHT, CALENDAR)
    )
