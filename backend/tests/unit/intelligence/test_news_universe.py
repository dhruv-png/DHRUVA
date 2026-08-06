"""The watchlist becomes exactly the names news reasoning is allowed to use.

One translation, in one place. If this drifts, entity linking and search
planning start disagreeing about what an instrument is called, and the symptom
appears three layers away as a headline that matched nothing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Final

import pytest

from dhruva.contexts.intelligence.application.universe import linkable_universe
from dhruva.contexts.reference.api import InstrumentKind, WatchlistInstrument
from dhruva.contexts.reference.domain.watchlist import (
    CashInstrumentMapping,
    InstrumentIdentityRevision,
    WatchlistMembershipRevision,
)
from dhruva.shared.identity import AccountId, InstrumentId

pytestmark = pytest.mark.unit

ACCOUNT: Final = AccountId.deterministic("owner-family")
RECORDED: Final = datetime(2026, 8, 1, 3, 0, tzinfo=UTC)
RENAMED_ON: Final = date(2025, 3, 1)


def _member(
    symbol: str,
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    former_names: tuple[str, ...] = (),
    valid_from: date = RENAMED_ON,
) -> WatchlistInstrument:
    instrument_id = InstrumentId.deterministic("reference", symbol)
    return WatchlistInstrument(
        identity=InstrumentIdentityRevision(
            instrument_id=instrument_id,
            kind=InstrumentKind.EQUITY,
            canonical_symbol=symbol,
            company_name=name,
            aliases=aliases,
            former_names=former_names,
            isin=None,
            sector="Financial Services",
            concentration_groups=(),
            cash_mapping=CashInstrumentMapping(exchange="NSE", trading_symbol=symbol),
            futures_research_requested=False,
            valid_from=valid_from,
            valid_to=None,
            recorded_at=RECORDED,
            source="owner-universe",
            source_revision="v1",
        ),
        membership=WatchlistMembershipRevision(
            account_id=ACCOUNT,
            instrument_id=instrument_id,
            active_from=valid_from,
            active_to=None,
            recorded_at=RECORDED,
            source="owner-universe",
            source_revision="v1",
        ),
    )


def test_the_identity_a_headline_is_matched_against_is_the_stable_one() -> None:
    """ADR-009: the symbol is an attribute; the instrument id is the identity."""
    projected = linkable_universe((_member("SBIN", "State Bank of India"),))

    assert projected[0].instrument_id == InstrumentId.deterministic("reference", "SBIN")
    assert projected[0].canonical_symbol == "SBIN"
    assert projected[0].company_name == "State Bank of India"


def test_aliases_survive_the_projection_unchanged() -> None:
    """An approved alias is owner configuration; nothing here may edit it."""
    projected = linkable_universe(
        (_member("TMPV", "Tata Motors Passenger Vehicles Limited", aliases=("Tata Motors PV",)),)
    )

    assert projected[0].aliases == ("Tata Motors PV",)


def test_a_former_name_ends_when_the_identity_that_replaced_it_began() -> None:
    """Reference records no end date, and the replacement revision is one.

    Deriving it from a recorded fact is the point: the alternative is inventing
    a date, and a former name with an invented expiry is a mapping that turns
    itself off on a day nobody chose.
    """
    projected = linkable_universe(
        (_member("ETERNAL", "Eternal Limited", former_names=("Zomato",), valid_from=RENAMED_ON),)
    )

    assert projected[0].former_names[0].text == "Zomato"
    assert projected[0].former_names[0].valid_to == RENAMED_ON


def test_an_instrument_with_no_history_projects_empty_collections() -> None:
    """Absence stays absence rather than becoming an empty-string alias."""
    projected = linkable_universe((_member("PNB", "Punjab National Bank"),))

    assert projected[0].aliases == ()
    assert projected[0].former_names == ()


def test_the_projection_is_ordered_by_canonical_symbol() -> None:
    """Two runs over the same watchlist must produce the same universe."""
    members = (
        _member("SBIN", "State Bank of India"),
        _member("CANBK", "Canara Bank"),
        _member("PNB", "Punjab National Bank"),
    )

    assert [entry.canonical_symbol for entry in linkable_universe(members)] == [
        "CANBK",
        "PNB",
        "SBIN",
    ]
    assert linkable_universe(members) == linkable_universe(tuple(reversed(members)))


def test_the_instrument_knows_when_its_identity_started() -> None:
    """Linking must not read a headline from before the company existed."""
    projected = linkable_universe((_member("ETERNAL", "Eternal Limited"),))

    assert projected[0].existed_on(RENAMED_ON) is True
    assert projected[0].existed_on(date(2024, 1, 1)) is False


def test_an_empty_watchlist_projects_an_empty_universe() -> None:
    """No members is a coherent answer rather than an error."""
    assert linkable_universe(()) == ()
