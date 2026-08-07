"""A small, interpretable summary of one instrument's recent daily bars.

Deliberately unambitious. Everything here is a subtraction or a ratio over
closes and volumes that are already stored, chosen so that a reader can check
any number by hand against the bars it came from. There are no indicators: a
moving average or an oscillator would look more sophisticated and would be a
judgement DHRUVA has not earned the right to make, sitting in a summary that
claims only to report.

Two rules do the work.

**Nothing is computed from a bar that is not there.** A one-day change needs two
closes; a five-day return needs a bar at least five sessions back. Where the
history is short the value is ``None`` and the reason is recorded, because a
zero would be read as "no change" and a fabricated baseline would be worse than
silence.

**Staleness is reported, never smoothed.** The latest stored bar may be days
before the cutoff — a holiday, a halt, or an ingestion that did not run. The
summary says which date it actually has and how many days old that is, and the
caller decides what to do about it. A summary that quietly presented Tuesday's
close as today's would be the most damaging thing in this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from dhruva.shared.errors import ValidationError
from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import date

    from dhruva.contexts.marketdata.domain.daily_bars import DailyBarRevision, DailyBarSeries
    from dhruva.shared.identity import InstrumentId

__all__ = [
    "DEFAULT_MULTI_DAY_SESSIONS",
    "DEFAULT_STALE_AFTER_DAYS",
    "MARKET_CONTEXT_REVISION",
    "MAX_MULTI_DAY_SESSIONS",
    "MarketContext",
    "MarketDataAvailability",
    "absent_context",
    "summarise_recent_bars",
]

#: Recorded on every summary, so a change of policy is visible in an operator's
#: output rather than only in this file's history.
MARKET_CONTEXT_REVISION = "market-context-v1"

#: Sessions in the bounded multi-day return. Five is one trading week, which is
#: the horizon a 2-20 session swing programme actually thinks in, and it is
#: short enough that the bars behind it are still individually checkable.
DEFAULT_MULTI_DAY_SESSIONS = 5
MAX_MULTI_DAY_SESSIONS = 60

#: Calendar days after which the newest stored bar is called stale. Four spans a
#: normal weekend plus one public holiday without crying wolf, and still flags a
#: refresh that silently stopped running on Monday.
DEFAULT_STALE_AFTER_DAYS = 4

#: Sessions averaged for the volume comparison, excluding the latest bar itself.
#: Twenty is roughly a trading month; fewer would make the baseline as noisy as
#: the thing it is a baseline for.
_VOLUME_BASELINE_SESSIONS = 20

#: Percentages and ratios are reported to this many places. Enough to be useful,
#: few enough that nobody mistakes the output for a precise measurement of
#: anything.
_PERCENT_PLACES = Decimal("0.01")
_RATIO_PLACES = Decimal("0.01")

_MINIMUM_BARS_FOR_CHANGE = 2


class MarketDataAvailability(StrEnum):
    """How much history the archive could offer at the cutoff.

    Deliberately *not* a place to record staleness. How many bars there are and
    how old the newest one is are independent facts -- a single bar can also be
    a month old -- and folding them into one field means one of them silently
    wins. Staleness is :attr:`MarketContext.is_stale`, computed from the dates.
    """

    #: At least two complete bars, so something can be compared.
    AVAILABLE = "AVAILABLE"
    #: Exactly one bar. A close with nothing to compare it against.
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    #: Nothing knowable at this cutoff. Not zero, not flat -- absent.
    NO_DATA = "NO_DATA"


@dataclass(frozen=True, slots=True)
class MarketContext:
    """Recent closes for one instrument, and an honest account of the gaps."""

    instrument_id: InstrumentId
    availability: MarketDataAvailability
    as_of: date
    #: Trading date of the newest bar knowable at the cutoff.
    latest_date: date | None = None
    latest_close: Decimal | None = None
    previous_close: Decimal | None = None
    #: Close-to-close percentage change against the previous stored session.
    one_day_change_percent: Decimal | None = None
    #: Percentage change over the requested number of sessions, when that many
    #: bars exist. ``None`` where the history is too short to support it.
    multi_day_change_percent: Decimal | None = None
    multi_day_sessions: int | None = None
    latest_volume: int | None = None
    #: Latest volume divided by the mean of the preceding sessions. Above one is
    #: heavier than recent normal; it is a ratio, not a signal.
    volume_ratio: Decimal | None = None
    volume_baseline_sessions: int | None = None
    #: Calendar days between the newest stored bar and the cutoff.
    staleness_days: int | None = None
    #: The bound this summary was judged against, carried so a reader can see
    #: which threshold produced the verdict rather than having to know it.
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS
    bars_available: int = 0
    #: Why anything absent is absent. Always populated when a value is missing.
    limitation: str | None = None
    revision: str = MARKET_CONTEXT_REVISION

    def __post_init__(self) -> None:
        """Refuse a summary that contradicts what it says it knows."""
        if self.availability is MarketDataAvailability.NO_DATA:
            invariant(self.latest_close is None, "an absent series cannot carry a close")
            invariant(self.bars_available == 0, "an absent series cannot carry bars")
            # Truthy, not merely non-None: an empty reason renders as
            # "no data -- " and is precisely the silent gap this type exists to
            # prevent.
            invariant(bool(self.limitation), "an absent series must say why")
        else:
            invariant(self.latest_close is not None, "a present series must carry a close")
            invariant(self.latest_date is not None, "a present series must carry its date")
            invariant(self.bars_available > 0, "a present series must carry bars")
        invariant(
            (self.one_day_change_percent is None) == (self.previous_close is None),
            "a one-day change and its baseline stand or fall together",
        )
        invariant(
            (self.multi_day_change_percent is None) == (self.multi_day_sessions is None),
            "a multi-day return must say how many sessions it covers",
        )

    @property
    def has_prices(self) -> bool:
        """Return whether any close is available to show."""
        return self.latest_close is not None

    @property
    def is_stale(self) -> bool:
        """Return whether the newest stored bar is older than the bound.

        Independent of :attr:`availability`: a series can be both too short to
        compare and too old to trust, and a reader needs to be told both.
        """
        return self.staleness_days is not None and self.staleness_days > self.stale_after_days


def absent_context(
    instrument_id: InstrumentId,
    *,
    as_of: date,
    reason: str,
) -> MarketContext:
    """Return the summary for an instrument with no knowable bars.

    A first-class value rather than ``None``, so a caller cannot accidentally
    render "no market data" and "market data we forgot to fetch" the same way.
    """
    return MarketContext(
        instrument_id=instrument_id,
        availability=MarketDataAvailability.NO_DATA,
        as_of=as_of,
        limitation=reason,
    )


def summarise_recent_bars(
    series: DailyBarSeries,
    *,
    as_of: date,
    sessions: int = DEFAULT_MULTI_DAY_SESSIONS,
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
) -> MarketContext:
    """Summarise the newest bars in ``series`` as they stood on ``as_of``.

    The series is assumed to have been read point-in-time already: this function
    performs no filtering by knowledge time, because doing it twice in two
    places is how the two eventually disagree. It does refuse a bar dated after
    ``as_of``, which would mean the caller's read and the caller's cutoff had
    come apart.

    Raises
    ------
    ValidationError
        If ``sessions`` is out of bounds, ``stale_after_days`` is negative, or
        the series contains a bar dated after ``as_of``.
    """
    if not 1 <= sessions <= MAX_MULTI_DAY_SESSIONS:
        raise ValidationError(
            "multi-day session count is outside its permitted bounds",
            sessions=sessions,
            maximum=MAX_MULTI_DAY_SESSIONS,
        )
    if stale_after_days < 0:
        raise ValidationError("a staleness bound cannot be negative", days=stale_after_days)

    bars = series.bars
    latest = bars[-1]
    latest_date = latest.candle.trading_date
    if latest_date > as_of:
        raise ValidationError(
            "a daily bar is dated after the cutoff it is summarised for",
            trading_date=latest_date.isoformat(),
            as_of=as_of.isoformat(),
        )

    staleness = (as_of - latest_date).days
    instrument_id = latest.instrument_id
    closes = tuple(bar.candle.close for bar in bars)

    if len(bars) < _MINIMUM_BARS_FOR_CHANGE:
        return MarketContext(
            instrument_id=instrument_id,
            availability=MarketDataAvailability.INSUFFICIENT_HISTORY,
            as_of=as_of,
            latest_date=latest_date,
            latest_close=closes[-1],
            latest_volume=latest.candle.volume,
            staleness_days=staleness,
            stale_after_days=stale_after_days,
            bars_available=len(bars),
            limitation="only one bar is knowable at this cutoff, so nothing can be compared",
        )

    previous_close = closes[-2]
    multi_day, covered, multi_day_note = _multi_day(closes, sessions=sessions)
    ratio, baseline = _volume_ratio(bars)

    limitations = [note for note in (multi_day_note,) if note is not None]
    if ratio is None:
        limitations.append("no earlier sessions to compare the latest volume against")

    return MarketContext(
        instrument_id=instrument_id,
        availability=MarketDataAvailability.AVAILABLE,
        as_of=as_of,
        latest_date=latest_date,
        latest_close=closes[-1],
        previous_close=previous_close,
        one_day_change_percent=_percent_change(previous_close, closes[-1]),
        multi_day_change_percent=multi_day,
        multi_day_sessions=covered,
        latest_volume=latest.candle.volume,
        volume_ratio=ratio,
        volume_baseline_sessions=baseline,
        staleness_days=staleness,
        stale_after_days=stale_after_days,
        bars_available=len(bars),
        limitation="; ".join(limitations) or None,
    )


def _percent_change(baseline: Decimal, current: Decimal) -> Decimal | None:
    """Return the percentage change, or ``None`` when it has no meaning.

    A zero or negative baseline cannot anchor a percentage. Prices are refused
    at construction so this should be unreachable, and it is handled anyway:
    a division that cannot be right must not be performed rather than produce a
    number somebody might act on.
    """
    if baseline <= 0:
        return None
    return (((current - baseline) / baseline) * Decimal(100)).quantize(_PERCENT_PLACES)


def _multi_day(
    closes: tuple[Decimal, ...],
    *,
    sessions: int,
) -> tuple[Decimal | None, int | None, str | None]:
    """Return the bounded multi-day return, or say why there is not one.

    Deliberately refuses to substitute a shorter window. "The five-day return,
    computed over three days because that is all we had" is a number whose label
    lies, and a caller comparing two instruments would be comparing different
    things without being told.
    """
    needed = sessions + 1
    if len(closes) < needed:
        return (
            None,
            None,
            f"{sessions}-session return needs {needed} bars and only {len(closes)} are stored",
        )
    return _percent_change(closes[-needed], closes[-1]), sessions, None


def _volume_ratio(bars: tuple[DailyBarRevision, ...]) -> tuple[Decimal | None, int | None]:
    """Return latest volume over the mean of the preceding sessions."""
    volumes = [bar.candle.volume for bar in bars]
    baseline = volumes[-1 - _VOLUME_BASELINE_SESSIONS : -1] or volumes[:-1]
    if not baseline:
        return None, None
    mean = Decimal(sum(baseline)) / Decimal(len(baseline))
    if mean <= 0:
        return None, len(baseline)
    return (Decimal(volumes[-1]) / mean).quantize(_RATIO_PLACES), len(baseline)
