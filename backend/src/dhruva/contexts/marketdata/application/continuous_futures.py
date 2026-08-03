"""Build the continuous futures research series from persisted actual contracts.

Derived on read, not stored. Every input is already persisted point-in-time, and
the roll rule is deterministic and versioned, so the series can be rebuilt
byte-for-byte from the facts and the policy revision that produced it. Storing a
second copy would add a table that can disagree with its own inputs, and the
first time it did the question "which one is right?" would have no answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dhruva.contexts.marketdata.domain.continuous_futures import (
    ContinuousFuturesSeries,
    FuturesContractTerms,
    RollPolicy,
    build_continuous_series,
)
from dhruva.contexts.marketdata.domain.daily_bars import BarCompleteness, DailyBarSeries
from dhruva.shared.errors import MissingDataError, ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import date, datetime

    from dhruva.contexts.marketdata.domain.ports import MarketDataUnitOfWork
    from dhruva.contexts.reference.api import FuturesContract
    from dhruva.shared.identity import AccountId, InstrumentId

__all__ = [
    "GetContinuousFuturesSeries",
    "GetContinuousFuturesSeriesQuery",
    "contract_terms",
]


def contract_terms(contracts: Iterable[FuturesContract]) -> tuple[FuturesContractTerms, ...]:
    """Translate reference contracts into the terms the roll rule needs.

    The translation lives here because this is the boundary. The roll rule works
    on expiries, lots and liquidity, and giving it the reference context's type
    would tie an arithmetic decision to another context's schema.
    """
    return tuple(
        FuturesContractTerms(
            contract_id=item.contract_id,
            instrument_token=item.instrument_token,
            expiry=item.expiry,
            lot_size=item.lot_size,
        )
        for item in contracts
    )


@dataclass(frozen=True, slots=True)
class GetContinuousFuturesSeriesQuery:
    """Point-in-time parameters for one deterministic continuous series."""

    account_id: AccountId
    underlying_id: InstrumentId
    benchmark_id: InstrumentId
    terms: tuple[FuturesContractTerms, ...]
    from_date: date
    to_date: date
    known_at: datetime
    # No default. The policy is part of the artefact's identity, and a versioned
    # research result whose version was chosen implicitly is the thing this
    # revision string exists to prevent.
    policy: RollPolicy


class GetContinuousFuturesSeries:
    """Read actual-contract history as of a knowledge time, then stitch it."""

    __slots__ = ("_unit_of_work_factory",)

    def __init__(
        self,
        unit_of_work_factory: Callable[[AccountId], MarketDataUnitOfWork],
    ) -> None:
        """Bind the query to a transaction factory."""
        self._unit_of_work_factory = unit_of_work_factory

    async def execute(
        self,
        query: GetContinuousFuturesSeriesQuery,
    ) -> ContinuousFuturesSeries:
        """Rebuild the series from persisted facts without look-ahead."""
        self._validate_query(query)
        async with self._unit_of_work_factory(query.account_id) as unit_of_work:
            benchmark = await self._read(unit_of_work, query, query.benchmark_id)
            if benchmark is None:
                raise MissingDataError(
                    "continuous series needs the benchmark session calendar",
                    benchmark_id=str(query.benchmark_id),
                )
            histories: dict[InstrumentId, DailyBarSeries] = {}
            for item in query.terms:
                history = await self._read(unit_of_work, query, item.contract_id)
                # A contract with no settled session in this window cannot
                # contribute a bar or decide a roll. Dropping it is the same
                # answer as never having listed it, and is not an error: a
                # far-month contract legitimately prints nothing early on.
                if history is not None:
                    histories[item.contract_id] = history

        terms = tuple(item for item in query.terms if item.contract_id in histories)
        if not terms:
            raise MissingDataError(
                "no requested futures contract has history in the range",
                underlying_id=str(query.underlying_id),
            )
        return build_continuous_series(
            underlying_id=query.underlying_id,
            terms=terms,
            histories=histories,
            sessions=_sessions(benchmark),
            policy=query.policy,
        )

    @staticmethod
    async def _read(
        unit_of_work: MarketDataUnitOfWork,
        query: GetContinuousFuturesSeriesQuery,
        instrument_id: InstrumentId,
    ) -> DailyBarSeries | None:
        """Read one instrument, tolerating absence but not incompleteness rules.

        ``require_complete`` is False on purpose: an unsettled bar at the right
        edge is a fact, and the roll rule discards it itself. Refusing the whole
        read would make the series unavailable on exactly the days it is wanted.
        """
        try:
            return await unit_of_work.daily_bars.list_series(
                instrument_id,
                from_date=query.from_date,
                to_date=query.to_date,
                known_at=query.known_at,
                require_complete=False,
            )
        except MissingDataError:
            return None

    @staticmethod
    def _validate_query(query: GetContinuousFuturesSeriesQuery) -> None:
        """Reject a query that cannot produce a deterministic answer."""
        if not query.terms:
            raise ValidationError("a continuous series needs at least one contract")
        if query.from_date > query.to_date:
            raise ValidationError("continuous series date range is reversed")
        if query.known_at.tzinfo is None or query.known_at.utcoffset() is None:
            raise ValidationError("known_at must be timezone-aware")
        identities = tuple(item.contract_id for item in query.terms)
        if len(identities) != len(set(identities)):
            raise ValidationError("continuous series contracts must be unique")
        if query.benchmark_id in set(identities):
            raise ValidationError("the benchmark cannot also be a futures contract")


def _sessions(benchmark: DailyBarSeries) -> tuple[date, ...]:
    """Use settled benchmark sessions as the calendar the roll rule counts in."""
    return tuple(
        bar.candle.trading_date
        for bar in benchmark.bars
        if bar.completeness is BarCompleteness.COMPLETE
    )
