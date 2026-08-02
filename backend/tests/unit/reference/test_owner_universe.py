"""The committed owner universe is exact, reviewable configuration."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from dhruva.contexts.reference.application.watchlist import UniverseDefinition
from dhruva.contexts.reference.domain.watchlist import InstrumentKind
from dhruva.contexts.reference.infrastructure.owner_universe import load_owner_universe
from dhruva.shared.identity import AccountId

pytestmark = pytest.mark.unit

APPROVED_SYMBOLS = (
    "ADANIENT",
    "ADANIPORTS",
    "ADANIGREEN",
    "ADANIENSOL",
    "ADANIPOWER",
    "HDFCAMC",
    "NAM-INDIA",
    "INDIGO",
    "ETERNAL",
    "CANBK",
    "SBIN",
    "PNB",
    "ICICIBANK",
    "BAJFINANCE",
    "ABCAPITAL",
    "TMPV",
    "M&M",
    "MAZDOCK",
    "HAL",
    "COCHINSHIP",
)

APPROVED_NAMES = {
    "ADANIENT": "Adani Enterprises Limited",
    "ADANIPORTS": "Adani Ports and Special Economic Zone Limited",
    "ADANIGREEN": "Adani Green Energy Limited",
    "ADANIENSOL": "Adani Energy Solutions Limited",
    "ADANIPOWER": "Adani Power Limited",
    "HDFCAMC": "HDFC Asset Management Company Limited",
    "NAM-INDIA": "Nippon Life India Asset Management Limited",
    "INDIGO": "InterGlobe Aviation Limited",
    "ETERNAL": "Eternal Limited",
    "CANBK": "Canara Bank",
    "SBIN": "State Bank of India",
    "PNB": "Punjab National Bank",
    "ICICIBANK": "ICICI Bank Limited",
    "BAJFINANCE": "Bajaj Finance Limited",
    "ABCAPITAL": "Aditya Birla Capital Limited",
    "TMPV": "Tata Motors Passenger Vehicles Limited",
    "M&M": "Mahindra & Mahindra Limited",
    "MAZDOCK": "Mazagon Dock Shipbuilders Limited",
    "HAL": "Hindustan Aeronautics Limited",
    "COCHINSHIP": "Cochin Shipyard Limited",
}


def _definitions() -> tuple[UniverseDefinition, ...]:
    command = load_owner_universe(
        AccountId.deterministic("owner-family"),
        recorded_at=datetime(2026, 8, 2, 12, tzinfo=UTC),
    )
    return command.definitions


def test_exact_owner_watchlist_and_names_are_preserved() -> None:
    """No security is added, removed, renamed or normalized."""
    definitions = _definitions()
    watchlist = tuple(item for item in definitions if item.included_in_watchlist)

    assert tuple(item.canonical_symbol for item in watchlist) == APPROVED_SYMBOLS
    assert {item.canonical_symbol: item.company_name for item in watchlist} == APPROVED_NAMES
    assert len(watchlist) == 20


def test_nam_india_and_mahindra_punctuation_is_exact() -> None:
    """The two explicitly protected symbols remain byte-for-byte exact."""
    symbols = {item.canonical_symbol for item in _definitions()}

    assert "NAM-INDIA" in symbols
    assert "M&M" in symbols
    assert "NAMINDIA" not in symbols
    assert "MANDM" not in symbols


def test_nifty_is_a_separate_non_watchlist_benchmark() -> None:
    """The benchmark exists without becoming a twenty-first stock pick."""
    nifty = next(item for item in _definitions() if item.canonical_symbol == "NIFTY 50")

    assert nifty.kind is InstrumentKind.INDEX
    assert not nifty.included_in_watchlist
    assert nifty.futures_research_requested


def test_aliases_and_historical_names_remain_separate_from_symbols() -> None:
    """News matching gets owner-approved aliases without replacing identity."""
    by_symbol = {item.canonical_symbol: item for item in _definitions()}

    assert "Adani Transmission" in by_symbol["ADANIENSOL"].former_names
    assert "Zomato" in by_symbol["ETERNAL"].former_names
    assert "Nippon India AMC" in by_symbol["NAM-INDIA"].aliases
    assert "Tata Motors PV" in by_symbol["TMPV"].aliases


def test_futures_are_requested_but_not_statically_declared_eligible() -> None:
    """Provider instrument masters, not this file, decide current eligibility."""
    watchlist = tuple(item for item in _definitions() if item.included_in_watchlist)

    assert all(item.futures_research_requested for item in watchlist)
    assert all(not hasattr(item, "futures_eligible") for item in watchlist)


def test_required_concentration_categories_are_exposed() -> None:
    """Sector and Adani-group metadata support later portfolio warnings."""
    by_symbol = {item.canonical_symbol: item for item in _definitions()}

    assert all(
        "Adani Group" in by_symbol[symbol].concentration_groups for symbol in APPROVED_SYMBOLS[:5]
    )
    assert {by_symbol[symbol].sector for symbol in ("CANBK", "SBIN", "PNB", "ICICIBANK")} == {
        "Banks"
    }
    assert by_symbol["M&M"].sector == "Automobiles"
    assert by_symbol["HAL"].sector == "Defence/Aerospace"
    assert by_symbol["MAZDOCK"].sector == "Shipbuilding"
    assert by_symbol["INDIGO"].sector == "Aviation"
    assert by_symbol["ADANIPOWER"].sector == "Energy/Utilities"
