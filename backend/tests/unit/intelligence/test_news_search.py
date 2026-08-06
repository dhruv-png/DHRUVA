"""Search phrases are deterministic, bounded, and never a bare ticker.

Every assertion here is about what DHRUVA will *ask a provider for*, which is
the last point at which a mistake is cheap. A phrase that is too broad turns
into evidence about the wrong company three layers downstream, where it looks
like a linking defect rather than a search one.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Final

import pytest

from dhruva.contexts.intelligence.domain.entity_linking import (
    FORMER_NAME_GRACE,
    HistoricalName,
    LinkableInstrument,
    MatchKind,
)
from dhruva.contexts.intelligence.domain.search import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    MAX_PHRASES_PER_INSTRUMENT,
    MIN_BATCH_SIZE,
    SEARCH_PLAN_REVISION,
    PhraseRejection,
    SearchPhrase,
    plan_search_phrases,
)
from dhruva.shared.errors import InvariantViolation, ValidationError
from dhruva.shared.identity import InstrumentId

pytestmark = pytest.mark.unit

TODAY: Final = date(2026, 8, 6)

#: The real owner-approved universe, read from the file the reference context
#: ships. A planner tested only against invented instruments is a planner that
#: has never met ``M&M`` or ``NAM-INDIA``.
_UNIVERSE_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "dhruva"
    / "contexts"
    / "reference"
    / "infrastructure"
    / "fixtures"
    / "owner_universe_v1.json"
)


def _instrument(
    symbol: str,
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    former: tuple[tuple[str, date], ...] = (),
) -> LinkableInstrument:
    return LinkableInstrument(
        instrument_id=InstrumentId.deterministic("reference", symbol),
        canonical_symbol=symbol,
        company_name=name,
        aliases=aliases,
        former_names=tuple(HistoricalName(text=text, valid_to=day) for text, day in former),
    )


@pytest.fixture(scope="module")
def owner_universe() -> tuple[LinkableInstrument, ...]:
    """Return the approved watchlist as linkable instruments."""
    raw = json.loads(_UNIVERSE_FIXTURE.read_text(encoding="utf-8"))
    return tuple(
        _instrument(
            entry["canonical_symbol"],
            entry["company_name"],
            aliases=tuple(entry["aliases"]),
            # Recent enough to still be inside the grace period on TODAY, so the
            # fixture exercises the eligible path rather than the expired one.
            former=tuple((name, date(2026, 3, 1)) for name in entry["former_names"]),
        )
        for entry in raw
    )


def _texts(instruments: tuple[LinkableInstrument, ...], **kwargs: int) -> tuple[str, ...]:
    plan = plan_search_phrases(instruments, on=TODAY, **kwargs)
    return tuple(text for batch in plan.batches for text in batch.texts)


def _refusals(
    instruments: tuple[LinkableInstrument, ...], symbol: str
) -> dict[str, PhraseRejection]:
    plan = plan_search_phrases(instruments, on=TODAY)
    return {
        refusal.text: refusal.reason
        for refusal in plan.rejected
        if refusal.canonical_symbol == symbol
    }


def test_the_company_name_is_the_first_phrase_for_every_instrument() -> None:
    """The most specific name an instrument has is the one queried first."""
    plan = plan_search_phrases((_instrument("PNB", "Punjab National Bank"),), on=TODAY)

    assert plan.batches[0].phrases[0] == SearchPhrase(
        text="Punjab National Bank",
        kind=MatchKind.COMPANY_NAME,
        instrument_id=InstrumentId.deterministic("reference", "PNB"),
        canonical_symbol="PNB",
    )


def test_a_corporate_suffix_is_dropped_when_a_usable_name_survives() -> None:
    """Headlines write "Adani Power", not "Adani Power Limited"."""
    assert _texts((_instrument("ADANIPOWER", "Adani Power Limited"),)) == ("Adani Power",)


def test_a_suffix_is_kept_when_dropping_it_would_leave_one_word() -> None:
    """Eternal Limited" must not decay into the adjective "eternal"."""
    assert _texts((_instrument("ETERNAL", "Eternal Limited"),)) == ("Eternal Limited",)


def test_a_bare_exchange_symbol_is_never_queried() -> None:
    """An alias that is exactly the ticker is refused, not searched."""
    universe = (_instrument("SBIN", "State Bank of India", aliases=("SBIN",)),)

    assert _texts(universe) == ("State Bank of India",)
    assert _refusals(universe, "SBIN") == {"SBIN": PhraseRejection.SYMBOL_SHAPED}


def test_a_short_all_capitals_alias_is_refused_as_ambiguous() -> None:
    """SBI" is the prefix of three separately listed companies."""
    universe = (_instrument("SBIN", "State Bank of India", aliases=("SBI",)),)

    assert _texts(universe) == ("State Bank of India",)
    assert _refusals(universe, "SBIN") == {"SBI": PhraseRejection.SYMBOL_SHAPED}


def test_a_mixed_case_brand_matching_the_symbol_is_still_queried() -> None:
    """``IndiGo`` is the name on the aircraft; ``INDIGO`` is the ticker.

    Folding case here would leave that airline searchable only by a holding
    company name that appears in no headline about it.
    """
    universe = (_instrument("INDIGO", "InterGlobe Aviation Limited", aliases=("IndiGo",)),)

    assert _texts(universe) == ("InterGlobe Aviation", "IndiGo")


def test_an_all_capitals_multi_word_alias_is_kept() -> None:
    """HDFC AMC" is upper case and is exactly how the press writes it."""
    universe = (
        _instrument("HDFCAMC", "HDFC Asset Management Company Limited", aliases=("HDFC AMC",)),
    )

    assert _texts(universe) == ("HDFC Asset Management Company", "HDFC AMC")


def test_a_phrase_carrying_query_syntax_is_refused_rather_than_escaped() -> None:
    """Escaping rules differ per provider; refusing does not."""
    universe = (_instrument("HAL", "Hindustan Aeronautics Limited", aliases=('HAL "the" one',)),)

    assert _refusals(universe, "HAL") == {'HAL "the" one': PhraseRejection.RESERVED_CHARACTER}


def test_only_two_phrases_are_kept_for_one_instrument() -> None:
    """The third spelling finds articles the first two already found."""
    universe = (
        _instrument(
            "ADANIENT",
            "Adani Enterprises Limited",
            aliases=("Adani Enterprise", "Adani Ent", "Adani Enterprises Ltd"),
        ),
    )
    plan = plan_search_phrases(universe, on=TODAY)

    assert plan.phrase_count == MAX_PHRASES_PER_INSTRUMENT
    assert PhraseRejection.BUDGET_EXHAUSTED in _refusals(universe, "ADANIENT").values()


def test_a_longer_alias_is_preferred_because_it_is_a_narrower_search() -> None:
    """Narrowing beats broadening when a wrong link is the expensive mistake."""
    universe = (
        _instrument("MAZDOCK", "Mazagon Dock Shipbuilders Limited", aliases=("MDL", "Mazagon")),
    )

    assert _texts(universe) == ("Mazagon Dock Shipbuilders", "Mazagon")


def test_a_duplicate_phrase_is_queried_once_and_the_repeat_is_reported() -> None:
    """Canara Bank" is both the company name and the alias."""
    universe = (_instrument("CANBK", "Canara Bank", aliases=("Canara Bank",)),)

    assert _texts(universe) == ("Canara Bank",)
    assert _refusals(universe, "CANBK") == {"Canara Bank": PhraseRejection.DUPLICATE}


def test_a_former_name_inside_its_grace_period_is_queried() -> None:
    """The press does not rename a company on the day its shareholders do."""
    universe = (_instrument("ETERNAL", "Eternal Limited", former=(("Zomato", TODAY),)),)

    assert _texts(universe) == ("Eternal Limited", "Zomato")


def test_an_expired_former_name_is_refused_and_still_reported() -> None:
    """A name reused by somebody else later must not inherit the mapping.

    It is reported rather than dropped: an operator who cannot see that
    "Zomato" stopped being searched cannot explain why coverage changed.
    """
    stale = TODAY - FORMER_NAME_GRACE - timedelta(days=1)
    universe = (_instrument("ETERNAL", "Eternal Limited", former=(("Zomato", stale),)),)

    assert _texts(universe) == ("Eternal Limited",)
    assert _refusals(universe, "ETERNAL") == {"Zomato": PhraseRejection.EXPIRED}


def test_an_instrument_with_no_safe_phrase_is_reported_as_unqueryable() -> None:
    """Silence would leave an operator believing coverage they do not have."""
    universe = (_instrument("XYZ", "XYZ", aliases=("XYZ",)),)
    plan = plan_search_phrases(universe, on=TODAY)

    assert plan.batches == ()
    assert len(plan.unqueryable) == 1
    assert plan.unqueryable[0].canonical_symbol == "XYZ"
    assert plan.unqueryable[0].instrument_id == InstrumentId.deterministic("reference", "XYZ")


def test_a_queryable_instrument_is_not_reported_as_unqueryable() -> None:
    """The report is about coverage, not about every refused candidate."""
    universe = (_instrument("SBIN", "State Bank of India", aliases=("SBI",)),)

    assert plan_search_phrases(universe, on=TODAY).unqueryable == ()


def test_the_plan_is_identical_whatever_order_the_watchlist_arrives_in(
    owner_universe: tuple[LinkableInstrument, ...],
) -> None:
    """A diff between two plans must mean the watchlist changed."""
    forward = plan_search_phrases(owner_universe, on=TODAY)
    backward = plan_search_phrases(tuple(reversed(owner_universe)), on=TODAY)

    assert forward == backward


def test_repeating_the_plan_produces_the_same_requests(
    owner_universe: tuple[LinkableInstrument, ...],
) -> None:
    """Two runs an hour apart must issue identical queries."""
    assert plan_search_phrases(owner_universe, on=TODAY) == plan_search_phrases(
        owner_universe, on=TODAY
    )


def test_batches_are_filled_in_order_and_numbered_from_one(
    owner_universe: tuple[LinkableInstrument, ...],
) -> None:
    """Batch numbering is what an operator uses to read a rate-limit report."""
    plan = plan_search_phrases(owner_universe, on=TODAY, batch_size=5)

    assert [batch.index for batch in plan.batches] == list(range(1, len(plan.batches) + 1))
    assert all(len(batch.phrases) == 5 for batch in plan.batches[:-1])


def test_the_final_batch_carries_the_remainder(
    owner_universe: tuple[LinkableInstrument, ...],
) -> None:
    """A partial last batch is still issued rather than silently dropped."""
    plan = plan_search_phrases(owner_universe, on=TODAY, batch_size=7)
    remainder = plan.phrase_count % 7

    assert len(plan.batches[-1].phrases) == (remainder or 7)
    assert plan.phrase_count == sum(len(batch.phrases) for batch in plan.batches)


def test_no_phrase_is_lost_or_repeated_across_batches(
    owner_universe: tuple[LinkableInstrument, ...],
) -> None:
    """Batching partitions the phrases; it does not resample them."""
    plan = plan_search_phrases(owner_universe, on=TODAY, batch_size=3)
    flattened = [phrase for batch in plan.batches for phrase in batch.phrases]

    assert len(flattened) == plan.phrase_count
    assert len({phrase.text.casefold() for phrase in flattened}) == len(flattened)


@pytest.mark.parametrize("size", [0, -1, MAX_BATCH_SIZE + 1, 1000])
def test_an_impossible_batch_size_is_refused(
    owner_universe: tuple[LinkableInstrument, ...], size: int
) -> None:
    """An unbounded batch is an unbounded demand on a free shared service."""
    with pytest.raises(ValidationError, match="batch size"):
        plan_search_phrases(owner_universe, on=TODAY, batch_size=size)


@pytest.mark.parametrize("size", [MIN_BATCH_SIZE, DEFAULT_BATCH_SIZE, MAX_BATCH_SIZE])
def test_the_permitted_batch_sizes_are_accepted(
    owner_universe: tuple[LinkableInstrument, ...], size: int
) -> None:
    """The advertised bounds are the bounds that work."""
    plan = plan_search_phrases(owner_universe, on=TODAY, batch_size=size)

    assert all(len(batch.phrases) <= size for batch in plan.batches)


def test_the_whole_approved_watchlist_is_queryable(
    owner_universe: tuple[LinkableInstrument, ...],
) -> None:
    """Every owner-approved instrument gets at least one phrase.

    If this fails, the watchlist gained an instrument whose name the rules
    cannot use, and the right response is to look at that instrument rather
    than to loosen the rules.
    """
    plan = plan_search_phrases(owner_universe, on=TODAY)

    assert plan.unqueryable == ()
    assert {phrase.canonical_symbol for batch in plan.batches for phrase in batch.phrases} == {
        instrument.canonical_symbol for instrument in owner_universe
    }


def test_no_planned_phrase_is_a_bare_symbol_from_the_approved_watchlist(
    owner_universe: tuple[LinkableInstrument, ...],
) -> None:
    """The rule that matters most, asserted against the real symbols."""
    symbols = {instrument.canonical_symbol for instrument in owner_universe}
    plan = plan_search_phrases(owner_universe, on=TODAY)

    for batch in plan.batches:
        for phrase in batch.phrases:
            assert phrase.text not in symbols
            assert phrase.kind is not MatchKind.CANONICAL_SYMBOL


def test_the_plan_records_the_ruleset_that_built_it(
    owner_universe: tuple[LinkableInstrument, ...],
) -> None:
    """A change of policy has to be visible in an operator's output."""
    assert plan_search_phrases(owner_universe, on=TODAY).revision == SEARCH_PLAN_REVISION


def test_a_phrase_refuses_to_be_constructed_from_a_symbol_match_kind() -> None:
    """The type enforces the rule the planner implements."""
    with pytest.raises(InvariantViolation, match="never a search phrase"):
        SearchPhrase(
            text="State Bank of India",
            kind=MatchKind.CANONICAL_SYMBOL,
            instrument_id=InstrumentId.deterministic("reference", "SBIN"),
            canonical_symbol="SBIN",
        )


def test_an_empty_watchlist_plans_nothing_rather_than_failing() -> None:
    """No instruments is a coherent answer, not an error."""
    plan = plan_search_phrases((), on=TODAY)

    assert plan.batches == ()
    assert plan.unqueryable == ()
    assert plan.phrase_count == 0
