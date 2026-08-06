"""Project the shared watchlist onto the names news reasoning matches against.

Reference owns what an instrument *is*; intelligence needs only what it might be
*called*. This is the whole of the translation, in one place, so that neither
entity linking nor search planning ever holds a reference type and neither has
to be re-tested when the reference model grows a field.

One derivation is worth naming. Reference records a former name as text with no
end date, because the identity revision that replaced it already carries the day
the change took effect. That day is what a former name's grace period is
measured from, so it is used as the former name's ``valid_to``. It is at worst a
day generous against a grace period measured in months, and it is derived from
a recorded fact rather than assumed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.domain.entity_linking import (
    HistoricalName,
    LinkableInstrument,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from dhruva.contexts.reference.api import WatchlistInstrument

__all__ = ["linkable_universe"]


def linkable_universe(
    watchlist: Iterable[WatchlistInstrument],
) -> tuple[LinkableInstrument, ...]:
    """Return the approved universe as intelligence needs to see it.

    Ordered by canonical symbol so that two calls over the same watchlist
    produce the same universe, and so a diff between two runs is a change in the
    watchlist rather than in a dictionary's iteration order.
    """
    return tuple(
        sorted(
            (_project(entry) for entry in watchlist),
            key=lambda instrument: instrument.canonical_symbol,
        )
    )


def _project(entry: WatchlistInstrument) -> LinkableInstrument:
    """Return one watchlist member as a linkable instrument."""
    identity = entry.identity
    return LinkableInstrument(
        instrument_id=identity.instrument_id,
        canonical_symbol=identity.canonical_symbol,
        company_name=identity.company_name,
        aliases=identity.aliases,
        former_names=tuple(
            HistoricalName(text=name, valid_to=identity.valid_from)
            for name in identity.former_names
        ),
        valid_from=identity.valid_from,
    )
