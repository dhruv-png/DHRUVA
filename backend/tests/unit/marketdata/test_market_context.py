"""Market context reports arithmetic over stored closes, and admits every gap.

The failure this guards against is not a wrong percentage. It is a *plausible*
percentage computed from a baseline that was not there — a five-day return over
three days of history, a change against a close from last month presented as
yesterday's. Every test below is about refusing to produce a number rather than
about producing one.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final

import pytest

from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    BarCompleteness,
    DailyBarRevision,
    DailyBarSeries,
    DailyCandle,
    MarketInstrumentKind,
)
from dhruva.contexts.marketdata.domain.market_context import (
    DEFAULT_MULTI_DAY_SESSIONS,
    DEFAULT_STALE_AFTER_DAYS,
    MARKET_CONTEXT_REVISION,
    MAX_MULTI_DAY_SESSIONS,
    MarketDataAvailability,
    absent_context,
    summarise_recent_bars,
)
from dhruva.shared.errors import InvariantViolation, ValidationError
from dhruva.shared.identity import InstrumentId

pytestmark = pytest.mark.unit

INSTRUMENT: Final = InstrumentId.deterministic("reference", "nse-equity-sbin")
CUTOFF: Final = date(2026, 8, 3)
RETRIEVED: Final = datetime(2026, 8, 3, 12, tzinfo=UTC)


def _bar(trading_date: date, close: str, volume: int = 1_000) -> DailyBarRevision:
    price = Decimal(close)
    return DailyBarRevision(
        instrument_id=INSTRUMENT,
        instrument_kind=MarketInstrumentKind.CASH_EQUITY,
        source="kite",
        source_instrument_id=1,
        candle=DailyCandle(
            trading_date=trading_date,
            open=price,
            high=price + 1,
            low=price - 1,
            close=price,
            volume=volume,
            open_interest=None,
        ),
        retrieved_at=RETRIEVED,
        adjustment_status=AdjustmentStatus.RAW,
        completeness=BarCompleteness.COMPLETE,
        source_revision="a" * 64,
        batch_sha256="b" * 64,
        quality_revision="daily-bar-quality-v1",
    )


def _series(
    closes: list[str], *, last: date = CUTOFF, volumes: list[int] | None = None
) -> DailyBarSeries:
    """Build consecutive daily bars ending on ``last``."""
    count = len(closes)
    picked = volumes or [1_000] * count
    return DailyBarSeries(
        bars=tuple(
            _bar(last - timedelta(days=count - 1 - index), close, picked[index])
            for index, close in enumerate(closes)
        )
    )


# --------------------------------------------------------------------------- #
# The ordinary case
# --------------------------------------------------------------------------- #


def test_the_latest_close_and_its_date_are_reported() -> None:
    """The single most useful fact, and the day it actually belongs to."""
    context = summarise_recent_bars(_series(["100", "110"]), as_of=CUTOFF)

    assert context.latest_close == Decimal("110")
    assert context.latest_date == CUTOFF
    assert context.previous_close == Decimal("100")


def test_the_one_day_change_is_close_over_previous_close() -> None:
    """Checkable by hand: 100 to 110 is +10%."""
    context = summarise_recent_bars(_series(["100", "110"]), as_of=CUTOFF)

    assert context.one_day_change_percent == Decimal("10.00")


def test_a_fall_is_reported_as_negative() -> None:
    """A sign error here would invert the one number a reader scans for."""
    context = summarise_recent_bars(_series(["110", "99"]), as_of=CUTOFF)

    assert context.one_day_change_percent == Decimal("-10.00")


def test_the_multi_day_return_spans_the_requested_sessions() -> None:
    """Six bars, five sessions: 100 to 105 is +5%."""
    context = summarise_recent_bars(
        _series(["100", "101", "102", "103", "104", "105"]), as_of=CUTOFF, sessions=5
    )

    assert context.multi_day_change_percent == Decimal("5.00")
    assert context.multi_day_sessions == 5


def test_the_volume_ratio_compares_the_latest_bar_to_the_ones_before_it() -> None:
    """Three times the recent mean is 3.00x, not a signal and not a verdict."""
    context = summarise_recent_bars(
        _series(["100", "101", "102"], volumes=[1_000, 1_000, 3_000]), as_of=CUTOFF
    )

    assert context.volume_ratio == Decimal("3.00")
    assert context.volume_baseline_sessions == 2


def test_a_complete_summary_reports_no_limitation() -> None:
    """The note exists to explain gaps; a full summary has none to explain."""
    context = summarise_recent_bars(
        _series([str(100 + n) for n in range(30)]), as_of=CUTOFF, sessions=5
    )

    assert context.limitation is None
    assert context.availability is MarketDataAvailability.AVAILABLE


# --------------------------------------------------------------------------- #
# Refusing to invent
# --------------------------------------------------------------------------- #


def test_one_bar_yields_a_close_and_nothing_derived() -> None:
    """A close with nothing to compare it against is exactly that."""
    context = summarise_recent_bars(_series(["100"]), as_of=CUTOFF)

    assert context.availability is MarketDataAvailability.INSUFFICIENT_HISTORY
    assert context.latest_close == Decimal("100")
    assert context.one_day_change_percent is None
    assert context.previous_close is None
    assert context.limitation is not None


def test_a_short_history_refuses_the_multi_day_return_rather_than_shortening_it() -> None:
    """A five-day return computed over three days is a label that lies.

    Substituting a shorter window would let a reader compare two instruments
    that were measured over different periods without being told.
    """
    context = summarise_recent_bars(_series(["100", "101", "102"]), as_of=CUTOFF, sessions=5)

    assert context.multi_day_change_percent is None
    assert context.multi_day_sessions is None
    assert context.limitation is not None
    assert "5-session" in context.limitation


def test_the_one_day_change_still_works_when_the_multi_day_one_cannot() -> None:
    """Missing one figure must not suppress the others."""
    context = summarise_recent_bars(_series(["100", "110"]), as_of=CUTOFF, sessions=5)

    assert context.one_day_change_percent == Decimal("10.00")
    assert context.multi_day_change_percent is None


def test_two_bars_compare_volume_against_the_single_prior_session() -> None:
    """A baseline of one is thin, and it is reported as one rather than hidden."""
    context = summarise_recent_bars(_series(["100", "101"], volumes=[500, 1_000]), as_of=CUTOFF)

    assert context.volume_ratio == Decimal("2.00")
    assert context.volume_baseline_sessions == 1


def test_an_absent_series_is_a_first_class_answer() -> None:
    """An absent series and an unfetched one must not render the same way."""
    context = absent_context(INSTRUMENT, as_of=CUTOFF, reason="nothing stored")

    assert context.availability is MarketDataAvailability.NO_DATA
    assert context.has_prices is False
    assert context.bars_available == 0
    assert context.limitation == "nothing stored"


# --------------------------------------------------------------------------- #
# Staleness, orthogonal to history
# --------------------------------------------------------------------------- #


def test_a_recent_series_is_not_stale() -> None:
    """A weekend must not be reported as an outage."""
    context = summarise_recent_bars(
        _series(["100", "110"], last=CUTOFF - timedelta(days=DEFAULT_STALE_AFTER_DAYS)),
        as_of=CUTOFF,
    )

    assert context.is_stale is False
    assert context.staleness_days == DEFAULT_STALE_AFTER_DAYS


def test_a_series_older_than_the_bound_is_stale() -> None:
    """Presenting last month's close as today's is the worst failure available."""
    context = summarise_recent_bars(
        _series(["100", "110"], last=CUTOFF - timedelta(days=DEFAULT_STALE_AFTER_DAYS + 1)),
        as_of=CUTOFF,
    )

    assert context.is_stale is True
    assert context.availability is MarketDataAvailability.AVAILABLE


def test_a_series_can_be_both_too_short_and_too_old() -> None:
    """The two facts are independent, so neither may mask the other.

    Folding staleness into the availability enum made exactly this case
    unreportable: a single month-old bar came back as INSUFFICIENT_HISTORY and
    silently not stale.
    """
    context = summarise_recent_bars(
        _series(["100"], last=CUTOFF - timedelta(days=30)), as_of=CUTOFF
    )

    assert context.availability is MarketDataAvailability.INSUFFICIENT_HISTORY
    assert context.is_stale is True
    assert context.staleness_days == 30


def test_the_staleness_bound_is_carried_so_the_verdict_can_be_checked() -> None:
    """A reader should not have to know the threshold to read the verdict."""
    context = summarise_recent_bars(_series(["100", "110"]), as_of=CUTOFF, stale_after_days=0)

    assert context.stale_after_days == 0
    assert context.is_stale is False


# --------------------------------------------------------------------------- #
# The cutoff is absolute
# --------------------------------------------------------------------------- #


def test_a_bar_dated_after_the_cutoff_is_refused() -> None:
    """The read and the cutoff would have come apart, and that must be loud.

    Filtering it out silently would hide a defect in the caller behind a
    plausible-looking summary.
    """
    with pytest.raises(ValidationError, match="dated after the cutoff"):
        summarise_recent_bars(_series(["100", "110"]), as_of=CUTOFF - timedelta(days=1))


def test_a_cutoff_on_the_latest_bar_date_is_accepted() -> None:
    """The bound is inclusive; today's close is knowable today."""
    context = summarise_recent_bars(_series(["100", "110"]), as_of=CUTOFF)

    assert context.staleness_days == 0


# --------------------------------------------------------------------------- #
# Bounds and self-consistency
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("sessions", [0, -1, MAX_MULTI_DAY_SESSIONS + 1])
def test_an_impossible_session_count_is_refused(sessions: int) -> None:
    """An unbounded window is an unbounded read."""
    with pytest.raises(ValidationError, match="session count"):
        summarise_recent_bars(_series(["100", "110"]), as_of=CUTOFF, sessions=sessions)


def test_a_negative_staleness_bound_is_refused() -> None:
    """A bar cannot be fresher than the day it is dated."""
    with pytest.raises(ValidationError, match="staleness bound"):
        summarise_recent_bars(_series(["100", "110"]), as_of=CUTOFF, stale_after_days=-1)


def test_a_summary_cannot_claim_a_close_it_does_not_have() -> None:
    """The type refuses to hold a state that contradicts itself."""
    with pytest.raises(InvariantViolation, match="absent series"):
        absent_context(INSTRUMENT, as_of=CUTOFF, reason="")


def test_the_summary_records_the_policy_that_built_it() -> None:
    """A change of method must be visible in an operator's output."""
    assert summarise_recent_bars(_series(["100", "110"]), as_of=CUTOFF).revision == (
        MARKET_CONTEXT_REVISION
    )


def test_summarising_the_same_bars_twice_gives_the_same_answer() -> None:
    """Arithmetic over stored values has no business being non-deterministic."""
    series = _series([str(100 + n) for n in range(10)])

    assert summarise_recent_bars(series, as_of=CUTOFF) == summarise_recent_bars(
        series, as_of=CUTOFF
    )


def test_the_default_session_count_is_the_documented_one() -> None:
    """One trading week, and one place that decides so."""
    context = summarise_recent_bars(_series([str(100 + n) for n in range(10)]), as_of=CUTOFF)

    assert context.multi_day_sessions == DEFAULT_MULTI_DAY_SESSIONS
