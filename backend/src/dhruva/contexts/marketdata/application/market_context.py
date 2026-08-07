"""Read recent bars point-in-time and summarise them, or say why there are none.

The read is the existing :class:`GetDailyBarSeries` with its ``known_at``
cutoff, so the point-in-time rule is enforced in exactly one place — the
repository — and this use case adds no filtering of its own. Duplicating the
cutoff logic here would give two implementations that agree until the day they
do not.

The one thing this adds is that **a missing series is an answer**. The
repository raises when an instrument has no knowable bars, which is right for a
caller that asked for that instrument's history and got nothing. It is wrong for
a caller assembling a report over twenty instruments, where three of them
legitimately having no stored bars is Tuesday. So the absence is caught, turned
into a ``NO_DATA`` summary carrying the reason, and reporting continues.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from dhruva.contexts.marketdata.application.daily_history import (
    GetDailyBarSeries,
    GetDailyBarSeriesQuery,
)
from dhruva.contexts.marketdata.domain.market_context import (
    DEFAULT_MULTI_DAY_SESSIONS,
    DEFAULT_STALE_AFTER_DAYS,
    absent_context,
    summarise_recent_bars,
)
from dhruva.shared.errors import MissingDataError, ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import date, datetime

    from dhruva.contexts.marketdata.domain.market_context import MarketContext
    from dhruva.contexts.marketdata.domain.ports import MarketDataUnitOfWork
    from dhruva.shared.identity import AccountId, InstrumentId

__all__ = ["GetMarketContext", "GetMarketContextQuery", "contexts_by_instrument"]

#: Calendar days of history read to compute the summary. Generous relative to
#: the five sessions and twenty-session volume baseline it feeds, because
#: weekends, holidays and halts all consume calendar days without producing
#: bars, and a window that only just fits produces a `None` the first time the
#: exchange closes for Diwali.
_LOOKBACK_DAYS = 90

#: Bounds the read regardless of what a caller asks for.
_MAX_LOOKBACK_DAYS = 400


@dataclass(frozen=True, slots=True)
class GetMarketContextQuery:
    """Which instruments, as known when, summarised as of which trading day."""

    account_id: AccountId
    instrument_ids: tuple[InstrumentId, ...]
    known_at: datetime
    sessions: int = DEFAULT_MULTI_DAY_SESSIONS
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS
    lookback_days: int = _LOOKBACK_DAYS


class GetMarketContext:
    """Summarise recent daily bars for each requested instrument."""

    __slots__ = ("_read",)

    def __init__(
        self,
        unit_of_work_factory: Callable[[AccountId], MarketDataUnitOfWork],
    ) -> None:
        """Bind the query to a transaction factory."""
        self._read = GetDailyBarSeries(unit_of_work_factory)

    async def execute(
        self,
        query: GetMarketContextQuery,
    ) -> tuple[MarketContext, ...]:
        """Return one summary per requested instrument, in the order requested.

        One summary per instrument, always — an instrument with no bars comes
        back as ``NO_DATA`` rather than being dropped, so a caller cannot mistake
        "we have no prices for this" for "we forgot to ask".

        Raises
        ------
        ValidationError
            If the lookback window is outside its bounds.
        """
        GetMarketContext._validate(query)
        as_of = query.known_at.date()
        window = timedelta(days=query.lookback_days)

        summaries = []
        for instrument_id in query.instrument_ids:
            summaries.append(
                await self._summarise(
                    query,
                    instrument_id=instrument_id,
                    from_date=(query.known_at - window).date(),
                    to_date=as_of,
                )
            )
        return tuple(summaries)

    async def _summarise(
        self,
        query: GetMarketContextQuery,
        *,
        instrument_id: InstrumentId,
        from_date: date,
        to_date: date,
    ) -> MarketContext:
        """Summarise one instrument, turning absence into a reported outcome."""
        as_of = query.known_at.date()
        try:
            series = await self._read.execute(
                GetDailyBarSeriesQuery(
                    account_id=query.account_id,
                    instrument_id=instrument_id,
                    from_date=from_date,
                    to_date=to_date,
                    known_at=query.known_at,
                    require_complete=True,
                )
            )
        except MissingDataError:
            return absent_context(
                instrument_id,
                as_of=as_of,
                reason=(
                    f"no complete daily bars knowable at {query.known_at.isoformat()} "
                    f"within {query.lookback_days} days"
                ),
            )
        return summarise_recent_bars(
            series,
            as_of=as_of,
            sessions=query.sessions,
            stale_after_days=query.stale_after_days,
        )

    @staticmethod
    def _validate(query: GetMarketContextQuery) -> None:
        """Reject a read that could not produce a usable summary."""
        if query.known_at.tzinfo is None or query.known_at.utcoffset() is None:
            raise ValidationError("known_at must be timezone-aware")
        if not 1 <= query.lookback_days <= _MAX_LOOKBACK_DAYS:
            raise ValidationError(
                "market-context lookback is outside its permitted bounds",
                lookback_days=query.lookback_days,
                maximum=_MAX_LOOKBACK_DAYS,
            )


def contexts_by_instrument(
    summaries: Sequence[MarketContext],
) -> dict[InstrumentId, MarketContext]:
    """Index summaries by instrument, for a caller joining them to something else."""
    return {summary.instrument_id: summary for summary in summaries}
