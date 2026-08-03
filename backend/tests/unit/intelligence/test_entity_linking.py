"""Entity linking against the owner-approved watchlist, aliases and former names."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from dhruva.contexts.intelligence.domain.entity_linking import (
    ENTITY_LINKING_REVISION,
    FORMER_NAME_GRACE,
    EntityLinkResult,
    HistoricalName,
    LinkableInstrument,
    MatchKind,
    MatchState,
    link_entities,
)
from dhruva.shared.identity import InstrumentId
from dhruva.shared.invariants import InvariantViolation

pytestmark = pytest.mark.unit

TODAY = date(2026, 8, 3)
RENAMED_ON = date(2026, 6, 30)


def _instrument(
    symbol: str,
    company_name: str,
    *,
    aliases: tuple[str, ...] = (),
    former_names: tuple[HistoricalName, ...] = (),
    valid_from: date | None = None,
) -> LinkableInstrument:
    return LinkableInstrument(
        instrument_id=InstrumentId.deterministic("reference", f"nse-equity-{symbol.lower()}"),
        canonical_symbol=symbol,
        company_name=company_name,
        aliases=aliases,
        former_names=former_names,
        valid_from=valid_from,
    )


#: The exact owner-approved names, including the two punctuation-sensitive
#: symbols and the historical names the press still uses.
SBIN = _instrument("SBIN", "State Bank of India", aliases=("SBI",))
PNB = _instrument("PNB", "Punjab National Bank")
CANBK = _instrument("CANBK", "Canara Bank")
ICICIBANK = _instrument("ICICIBANK", "ICICI Bank Limited")
BAJFINANCE = _instrument("BAJFINANCE", "Bajaj Finance Limited")
MM = _instrument("M&M", "Mahindra & Mahindra Limited", aliases=("Mahindra & Mahindra",))
NAM_INDIA = _instrument(
    "NAM-INDIA",
    "Nippon Life India Asset Management Limited",
    aliases=("NAM India", "Nippon India AMC"),
)
INDIGO = _instrument("INDIGO", "InterGlobe Aviation Limited", aliases=("IndiGo",))
ETERNAL = _instrument(
    "ETERNAL",
    "Eternal Limited",
    former_names=(HistoricalName("Zomato", RENAMED_ON),),
)
ADANIENSOL = _instrument(
    "ADANIENSOL",
    "Adani Energy Solutions Limited",
    aliases=("Adani Energy Solutions",),
    former_names=(HistoricalName("Adani Transmission", RENAMED_ON),),
)
TMPV = _instrument(
    "TMPV",
    "Tata Motors Passenger Vehicles Limited",
    aliases=("Tata Motors Passenger Vehicles", "Tata Motors PV"),
)
HAL = _instrument("HAL", "Hindustan Aeronautics Limited", aliases=("Hindustan Aeronautics",))

UNIVERSE = (
    SBIN,
    PNB,
    CANBK,
    ICICIBANK,
    BAJFINANCE,
    MM,
    NAM_INDIA,
    INDIGO,
    ETERNAL,
    ADANIENSOL,
    TMPV,
    HAL,
)


def _link(
    text: str,
    *,
    published_on: date = TODAY,
    universe: tuple[LinkableInstrument, ...] = (),
) -> EntityLinkResult:
    return link_entities(text, universe=universe or UNIVERSE, published_on=published_on)


def _symbols(
    text: str,
    *,
    published_on: date = TODAY,
    universe: tuple[LinkableInstrument, ...] = (),
) -> tuple[str, ...]:
    result = _link(text, published_on=published_on, universe=universe)
    return tuple(match.canonical_symbol for match in result.matches)


# --------------------------------------------------------------------------- #
# Every approved way in
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("SBIN shares gain 2%", "SBIN"),
        ("SBI reports higher quarterly profit", "SBIN"),
        ("State Bank of India cuts lending rates", "SBIN"),
        ("M&M unveils a new SUV", "M&M"),
        ("Mahindra & Mahindra posts record sales", "M&M"),
        ("NAM-INDIA assets under management rise", "NAM-INDIA"),
        ("NAM India sees strong inflows this quarter", "NAM-INDIA"),
        ("Nippon India AMC launches a new fund", "NAM-INDIA"),
        ("IndiGo adds new international routes", "INDIGO"),
        ("InterGlobe Aviation Limited reports a profit", "INDIGO"),
        ("ETERNAL shares gain 4%", "ETERNAL"),
        ("Eternal Limited reports its first annual profit", "ETERNAL"),
        ("Tata Motors Passenger Vehicles opens a new plant", "TMPV"),
        ("Tata Motors PV expands its EV range", "TMPV"),
        ("HAL delivers more Tejas jets", "HAL"),
        ("Hindustan Aeronautics bags an order", "HAL"),
        ("ICICI Bank posts higher net interest income", "ICICIBANK"),
        ("Bajaj Finance approves a dividend", "BAJFINANCE"),
        ("Adani Energy Solutions wins a project", "ADANIENSOL"),
    ],
)
def test_every_approved_name_reaches_its_canonical_symbol(headline: str, expected: str) -> None:
    """Aliases are additional ways in. They never replace the canonical symbol."""
    assert _symbols(headline) == (expected,)


def test_punctuation_sensitive_symbols_survive_tokenising() -> None:
    """``M&M`` and ``NAM-INDIA`` must not become ``m``, ``m`` and ``nam``, ``india``."""
    assert _symbols("M&M and NAM-INDIA both report today") == ("M&M", "NAM-INDIA")


def test_several_companies_in_one_headline_are_several_matches() -> None:
    """Two companies named is two findings, not an ambiguity."""
    result = _link("PNB and CANBK report improved asset quality")

    assert result.state is MatchState.MATCHED
    assert tuple(match.canonical_symbol for match in result.matches) == ("CANBK", "PNB")


def test_the_way_a_company_was_named_is_recorded_with_a_bounded_relevance() -> None:
    """A ticker is stronger evidence than a nickname, and the score says so."""
    symbol = _link("SBIN shares gain 2%").matches[0]
    alias = _link("SBI reports higher quarterly profit").matches[0]
    name = _link("State Bank of India cuts lending rates").matches[0]

    assert symbol.kind is MatchKind.CANONICAL_SYMBOL
    assert alias.kind is MatchKind.ALIAS
    assert name.kind is MatchKind.COMPANY_NAME
    assert symbol.relevance == Decimal("1.00")
    assert name.relevance == Decimal("0.90")
    assert alias.relevance == Decimal("0.80")
    assert Decimal(0) < alias.relevance <= Decimal(1)


# --------------------------------------------------------------------------- #
# False positives, which are the expensive mistake
# --------------------------------------------------------------------------- #


def test_a_symbol_that_is_an_ordinary_word_needs_its_ticker_spelling() -> None:
    """``ETERNAL`` is a company. "eternal optimism" is a mood."""
    assert _link("eternal optimism grips Dalal Street traders").state is MatchState.UNRESOLVED
    assert _symbols("ETERNAL shares gain 4%") == ("ETERNAL",)


def test_a_symbol_inside_a_longer_word_does_not_match() -> None:
    """``HALT`` is not ``HAL`` with a letter on the end."""
    assert _link("HALT in trading for several counters").state is MatchState.UNRESOLVED


def test_a_shouted_headline_cannot_be_read_for_tickers() -> None:
    """When every letter is capital, capitals prove nothing about tickers."""
    result = _link("PNB AND CANBK REPORT STRONG QUARTERS TODAY ACROSS THE BOARD")

    assert result.state is MatchState.UNRESOLVED


def test_a_shorter_company_name_does_not_match_a_longer_one() -> None:
    """A bare "Tata Motors" is a different listed company from Tata Motors PV."""
    assert _link("Tata Motors bags a large fleet order").state is MatchState.UNRESOLVED


def test_a_corporate_suffix_is_only_dropped_when_a_real_name_survives() -> None:
    """Shortening "ICICI Bank Limited" is safe; "Eternal Limited" would not be."""
    assert _symbols("ICICI Bank posts higher net interest income") == ("ICICIBANK",)
    assert _link("an eternal problem for policymakers").state is MatchState.UNRESOLVED


def test_a_headline_naming_nothing_approved_is_unresolved() -> None:
    """Unresolved is a real answer and carries no candidates at all."""
    result = _link("Some unrelated company announces quarterly results")

    assert result.state is MatchState.UNRESOLVED
    assert result.matches == ()
    assert result.ambiguous == ()
    assert result.reason


# --------------------------------------------------------------------------- #
# Ambiguity
# --------------------------------------------------------------------------- #


def test_one_wording_naming_two_instruments_is_ambiguous_not_two_findings() -> None:
    """A confident wrong link is worse than no link, so neither is presented."""
    first = _instrument("AAAA", "Shared Holdings Limited", aliases=("Shared Group",))
    second = _instrument("BBBB", "Other Holdings Limited", aliases=("Shared Group",))

    result = _link("Shared Group announces a restructuring", universe=(first, second))

    assert result.state is MatchState.AMBIGUOUS
    assert result.matches == ()
    assert tuple(match.canonical_symbol for match in result.ambiguous) == ("AAAA", "BBBB")
    assert "more than one" in result.reason


def test_an_ambiguous_wording_does_not_suppress_a_confident_match() -> None:
    """The clear name still resolves; the contested one is reported separately."""
    first = _instrument("AAAA", "Shared Holdings Limited", aliases=("Shared Group",))
    second = _instrument("BBBB", "Other Holdings Limited", aliases=("Shared Group",))

    result = _link(
        "Shared Group and Hindustan Aeronautics sign a pact",
        universe=(first, second, HAL),
    )

    assert result.state is MatchState.MATCHED
    assert tuple(match.canonical_symbol for match in result.matches) == ("HAL",)
    assert tuple(match.canonical_symbol for match in result.ambiguous) == ("AAAA", "BBBB")


# --------------------------------------------------------------------------- #
# Effective periods
# --------------------------------------------------------------------------- #


def test_a_former_name_still_links_while_the_press_is_still_using_it() -> None:
    """Nobody renames a company in print on the day its shareholders do."""
    result = _link("Zomato rebrands as Eternal", published_on=RENAMED_ON)

    assert result.matches[0].canonical_symbol == "ETERNAL"
    assert result.matches[0].kind is MatchKind.FORMER_NAME
    assert result.matches[0].relevance == Decimal("0.60")


def test_a_former_name_stops_linking_once_its_grace_period_ends() -> None:
    """A name reused by somebody else later must not inherit the mapping."""
    inside = RENAMED_ON + FORMER_NAME_GRACE
    outside = inside + FORMER_NAME_GRACE

    assert _symbols("Adani Transmission wins a project", published_on=inside) == ("ADANIENSOL",)
    assert _link("Adani Transmission wins a project", published_on=outside).state is (
        MatchState.UNRESOLVED
    )


def test_a_name_does_not_link_before_its_instrument_existed() -> None:
    """An identity that did not yet apply cannot be what a headline meant."""
    listed = _instrument("NEWCO", "Newco Industries Limited", valid_from=date(2026, 7, 1))

    assert _link("Newco Industries reports results", published_on=date(2026, 6, 1)).state is (
        MatchState.UNRESOLVED
    )
    assert (
        _link(
            "Newco Industries reports results",
            published_on=date(2026, 7, 2),
            universe=(listed,),
        ).state
        is MatchState.MATCHED
    )


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #


def test_every_result_names_its_ruleset_revision() -> None:
    """A stored mapping produced by an older rule must be visibly older."""
    assert _link("SBIN shares gain 2%").revision == ENTITY_LINKING_REVISION
    assert _link("nothing relevant here at all").revision == ENTITY_LINKING_REVISION


def test_every_match_gives_a_reason() -> None:
    """Mapping evidence is versioned and readable, per the MVP plan."""
    for match in _link("PNB and CANBK report improved asset quality").matches:
        assert match.reason
        assert match.matched_text


def test_linking_is_deterministic() -> None:
    """The same headline maps identically in every process and every run."""
    assert _link("SBI and HAL sign a pact") == _link("SBI and HAL sign a pact")


def test_a_canonical_symbol_must_be_uppercase() -> None:
    """Aliases never replace the canonical symbol, so the canonical form is fixed."""
    with pytest.raises(InvariantViolation, match="must be uppercase"):
        _instrument("sbin", "State Bank of India")
