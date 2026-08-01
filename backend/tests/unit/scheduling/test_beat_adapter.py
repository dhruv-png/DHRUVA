"""The Celery beat adapter (ADR-066).

Asserts translation, not policy. Whether a job runs on Diwali is
``TradingDaySchedule``'s business and is tested beside it; what is tested here is
that beat's ``is_due`` question gets the right answer from that policy and the
right *shape* of answer back.

Nothing sleeps. Celery's ``BaseSchedule`` takes a ``nowfun``, so every "now" in
this module is supplied (ADR-011).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from dhruva.contexts.platform.domain.scheduling import SessionAnchor, TradingDaySchedule
from dhruva.shared.errors import ValidationError
from dhruva.shared.time import SessionKind
from dhruva.workers.beat import IDLE_SLEEP, MAX_SLEEP, TradingDayBeatSchedule
from tests.unit.scheduling.test_trading_day_schedule import (
    CALENDAR,
    OPEN_UTC,
    instant,
)

pytestmark = pytest.mark.unit

#: A Thursday in the stub calendar's fortnight, opening at 09:15 IST.
THURSDAY = date(2026, 11, 5)
FRIDAY = date(2026, 11, 6)
#: The next session after Friday: Monday the 9th is a declared holiday.
AFTER_THE_HOLIDAY = date(2026, 11, 10)

AT_OPEN = TradingDaySchedule(anchor=SessionAnchor.MARKET_OPEN)


def beat(now: datetime, schedule: TradingDaySchedule = AT_OPEN) -> TradingDayBeatSchedule:
    """Build a beat schedule whose clock is frozen at ``now``."""
    return TradingDayBeatSchedule(schedule, CALENDAR, nowfun=lambda: now)


def test_a_job_whose_moment_has_arrived_is_due() -> None:
    """The base case: beat asked at the open, and the open is the moment."""
    verdict = beat(instant(THURSDAY, OPEN_UTC)).is_due(instant(THURSDAY, timedelta(hours=1)))

    assert verdict.is_due


def test_a_job_whose_moment_has_not_arrived_is_not_due() -> None:
    """Asked an hour before the open, beat must wait rather than run."""
    verdict = beat(instant(THURSDAY, OPEN_UTC - timedelta(hours=1))).is_due(
        instant(THURSDAY, timedelta(0))
    )

    assert not verdict.is_due


def test_the_wait_is_the_time_remaining_when_that_is_short() -> None:
    """Beat sleeps exactly as long as it should, not a rounded guess."""
    verdict = beat(instant(THURSDAY, OPEN_UTC - timedelta(minutes=2))).is_due(
        instant(THURSDAY, timedelta(0))
    )

    assert verdict.next == pytest.approx(timedelta(minutes=2).total_seconds())


def test_the_wait_is_capped_however_far_away_the_moment_is() -> None:
    """Beat holds the calendar it was given, and calendars change underneath it.

    Sleeping until Friday would mean not noticing a holiday declared on
    Wednesday, a clock correction, or a redeployment. Five minutes of
    re-evaluation costs nothing and removes a class of "the job did not run and
    nothing logged anything" incidents.
    """
    verdict = beat(instant(THURSDAY, timedelta(0))).is_due(instant(date(2026, 11, 4), timedelta(0)))

    assert not verdict.is_due
    assert verdict.next == pytest.approx(MAX_SLEEP.total_seconds())


def test_a_schedule_that_never_qualifies_reports_the_idle_wait() -> None:
    """A Muhurat-only job in a week with no Muhurat must answer, not search on."""
    muhurat_only = TradingDaySchedule(
        anchor=SessionAnchor.MARKET_OPEN, kinds=frozenset({SessionKind.MUHURAT})
    )

    verdict = beat(instant(date(2026, 12, 1), timedelta(0)), muhurat_only).is_due(
        instant(date(2026, 12, 1), timedelta(0))
    )

    assert not verdict.is_due
    assert verdict.next == pytest.approx(IDLE_SLEEP.total_seconds())


def test_a_beat_that_was_down_over_a_firing_catches_it_rather_than_skipping() -> None:
    """The next firing is sought after ``last_run_at``, not after now.

    A worker restarted at eleven must still run the nine-fifteen job. Searching
    from *now* would silently skip it and wait for tomorrow -- and the missing run
    would look like a schedule that had never been configured.
    """
    verdict = beat(instant(THURSDAY, timedelta(hours=6))).is_due(instant(THURSDAY, timedelta(0)))

    assert verdict.is_due


def test_a_holiday_is_skipped_without_beat_knowing_what_a_holiday_is() -> None:
    """The point of ADR-066, seen from beat's side.

    Asked on Friday evening, the next firing is the Tuesday session -- the
    weekend and the declared Monday holiday pass without beat containing a single
    statement about either.
    """
    friday_evening = instant(FRIDAY, timedelta(hours=13))

    verdict = beat(friday_evening).is_due(instant(FRIDAY, timedelta(hours=12)))

    assert not verdict.is_due
    assert verdict.next == pytest.approx(MAX_SLEEP.total_seconds())

    at_the_next_open = instant(AFTER_THE_HOLIDAY, OPEN_UTC)
    assert beat(at_the_next_open).is_due(friday_evening).is_due


def test_a_due_verdict_still_reports_a_sleep() -> None:
    """Beat wants a number even when the answer is "run it now"."""
    verdict = beat(instant(THURSDAY, OPEN_UTC)).is_due(instant(THURSDAY, timedelta(0)))

    assert verdict.is_due
    assert 0 < verdict.next <= MAX_SLEEP.total_seconds()


def test_a_naive_last_run_is_refused() -> None:
    """ADR-006. Comparing a naive instant to a session boundary is a category error.

    Refused loudly rather than coerced. Assuming UTC would be right on this
    deployment and wrong on the first one that is not, and the failure would be a
    job running at the wrong hour rather than anything that looked like an error.
    """
    with pytest.raises(ValidationError):
        beat(instant(THURSDAY, OPEN_UTC)).is_due(datetime(2026, 11, 5, 9, 15))  # noqa: DTZ001


def test_a_naive_clock_is_read_as_utc() -> None:
    """Celery returns a naive ``now`` when ``enable_utc`` is off.

    Tolerated where ``last_run_at`` is not, and the asymmetry is deliberate: the
    clock is *this* process's and its timezone is a configuration mistake we can
    interpret, while ``last_run_at`` was persisted by some earlier process whose
    timezone we would be guessing at.
    """
    naive_beat = TradingDayBeatSchedule(
        AT_OPEN,
        CALENDAR,
        nowfun=lambda: datetime(2026, 11, 5, 3, 45),  # noqa: DTZ001
    )

    assert naive_beat.is_due(instant(THURSDAY, timedelta(0))).is_due


def test_an_overnight_firing_from_yesterdays_session_is_not_missed() -> None:
    """The search span starts a day early, and this is why.

    A job anchored to the Muhurat close plus twelve hours fires the following
    morning. A span beginning today selects sessions *by date*, so it would not
    contain the session that produced the firing, and the job would never run.
    """
    overnight = TradingDaySchedule(anchor=SessionAnchor.MARKET_CLOSE, offset=timedelta(hours=12))
    # Muhurat on the 11th closes at 19:15 IST; twelve hours later is 07:15 IST
    # on the 12th, which is 01:45 UTC.
    firing = instant(date(2026, 11, 12), timedelta(hours=1, minutes=45))

    verdict = beat(firing, overnight).is_due(instant(date(2026, 11, 11), timedelta(hours=14)))

    assert verdict.is_due


# --------------------------------------------------------------------------- #
# What beat needs of the object itself
# --------------------------------------------------------------------------- #


def test_two_beats_with_the_same_policy_compare_equal() -> None:
    """Beat compares entries to decide whether a schedule changed since last run.

    Without this, every restart would look like a change and would reset every
    job's ``last_run_at`` -- which would then re-run whatever the reset made look
    overdue.
    """
    first = TradingDayBeatSchedule(AT_OPEN, CALENDAR)
    second = TradingDayBeatSchedule(AT_OPEN, CALENDAR)

    assert first == second
    assert len({first, second}) == 1


def test_a_beat_with_a_different_policy_does_not_compare_equal() -> None:
    """Equality must discriminate, or the comparison above asserts nothing."""
    other = TradingDaySchedule(anchor=SessionAnchor.MARKET_CLOSE)

    assert TradingDayBeatSchedule(AT_OPEN, CALENDAR) != TradingDayBeatSchedule(other, CALENDAR)


def test_a_beat_is_not_equal_to_an_unrelated_object() -> None:
    """``NotImplemented`` rather than ``False``, so Python can try the reflected side."""
    assert TradingDayBeatSchedule(AT_OPEN, CALENDAR) != "market_open"


def test_a_beat_survives_the_schedule_file() -> None:
    """Beat pickles its entries into a shelve between runs.

    An entry that could not be reconstructed would take the whole schedule file
    with it, and beat would start with no jobs and no complaint.
    """
    import pickle  # noqa: PLC0415 - only this test needs it

    original = TradingDayBeatSchedule(AT_OPEN, CALENDAR)

    revived = pickle.loads(pickle.dumps(original))  # noqa: S301 - our own bytes

    assert revived == original, (
        "an entry that compared unequal to itself after a round trip would make "
        "beat treat every restart as a schedule change and reset last_run_at"
    )
    assert hash(revived) == hash(original)


def test_a_beat_describes_itself_for_an_operator() -> None:
    """A beat listing full of ``<object at 0x...>`` tells nobody anything."""
    schedule = TradingDaySchedule(anchor=SessionAnchor.MARKET_CLOSE, offset=-timedelta(minutes=15))

    assert repr(TradingDayBeatSchedule(schedule, CALENDAR)) == (
        "<trading-day: market_close - 0:15:00>"
    )
