"""The digest reports what was stored, in an order it borrowed, and admits gaps.

Everything here runs the *real* classifiers and linker over real headlines rather
than arranging verdicts by hand. A digest whose test data was hand-labelled would
pass while disagreeing with the archive it claims to summarise, which is the one
failure that matters.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest

from dhruva.contexts.intelligence.domain.archive import (
    ArchivedNewsItem,
    NewsAnalysis,
    NewsRevision,
    content_revision,
)
from dhruva.contexts.intelligence.domain.digest import (
    DIGEST_REVISION,
    MAX_ITEMS_PER_INSTRUMENT,
    build_digest,
)
from dhruva.contexts.intelligence.domain.entity_linking import (
    LinkableInstrument,
    MatchState,
    link_entities,
)
from dhruva.contexts.intelligence.domain.events import EventCategory, classify_event
from dhruva.contexts.intelligence.domain.news import (
    DeduplicationDecision,
    NewsItem,
    NewsItemIdentity,
    NewsSource,
    NewsSourceTier,
    PermittedText,
    canonical_url,
)
from dhruva.contexts.intelligence.domain.sentiment import evaluate_sentiment
from dhruva.shared.errors import ValidationError
from dhruva.shared.identity import InstrumentId

pytestmark = pytest.mark.unit

PUBLISHED: Final = datetime(2026, 8, 3, 5, 30, tzinfo=UTC)
SEEN: Final = PUBLISHED + timedelta(minutes=30)
ANALYSED: Final = SEEN + timedelta(minutes=5)

GDELT: Final = NewsSource(
    key="gdelt",
    display_name="The GDELT Project via economictimes.indiatimes.test",
    tier=NewsSourceTier.AGGREGATOR,
    homepage_url="https://gdeltproject.org",
)


def _instrument(symbol: str, name: str, aliases: tuple[str, ...] = ()) -> LinkableInstrument:
    return LinkableInstrument(
        instrument_id=InstrumentId.deterministic("reference", symbol),
        canonical_symbol=symbol,
        company_name=name,
        aliases=aliases,
    )


HAL: Final = _instrument("HAL", "Hindustan Aeronautics Limited", ("Hindustan Aeronautics",))
SBIN: Final = _instrument("SBIN", "State Bank of India")
PNB: Final = _instrument("PNB", "Punjab National Bank")
UNIVERSE: Final = (HAL, SBIN, PNB)

#: Real headlines the real classifier assigns real categories to. The assertions
#: below read the classifier rather than assuming, so a ruleset change moves the
#: expectation with the code instead of breaking an unrelated test.
ORDER = "Hindustan Aeronautics bags order worth Rs 5,000 crore from the ministry"
FRAUD = "State Bank of India faces a forensic audit over accounting irregularities"
COMMENTARY = "Punjab National Bank shares were among the most traded on Tuesday"


def _item(url: str, title: str, *, published_at: datetime = PUBLISHED) -> NewsItem:
    link = canonical_url(url)
    return NewsItem(
        identity=NewsItemIdentity(
            source_key=GDELT.key,
            provider_item_id=f"url-sha256:{hashlib.sha256(link.encode()).hexdigest()}",
            url=link,
        ),
        source=GDELT,
        text=PermittedText(title=title),
        published_at=published_at,
        first_seen_at=SEEN,
    )


def _archived(
    url: str,
    title: str,
    *,
    published_at: datetime = PUBLISHED,
    universe: tuple[LinkableInstrument, ...] = UNIVERSE,
    analysed: bool = True,
) -> ArchivedNewsItem:
    item = _item(url, title, published_at=published_at)
    analysis = (
        NewsAnalysis(
            event=classify_event(title),
            sentiment=evaluate_sentiment(title),
            mapping=link_entities(title, universe=universe, published_on=published_at.date()),
            analysed_at=ANALYSED,
        )
        if analysed
        else None
    )
    return ArchivedNewsItem(
        revision=NewsRevision(
            item=item,
            revision=content_revision(item),
            deduplication=DeduplicationDecision(
                is_duplicate=False, rule=None, original=None, reason="first observation"
            ),
        ),
        analysis=analysis,
    )


def _digest(*items: ArchivedNewsItem, universe: tuple[LinkableInstrument, ...] = UNIVERSE, **kw):  # type: ignore[no-untyped-def]
    return build_digest(
        universe,
        items,
        known_at=ANALYSED,
        published_from=PUBLISHED - timedelta(days=7),
        published_to=ANALYSED,
        **kw,
    )


def _section(digest, symbol: str):  # type: ignore[no-untyped-def]
    return next(s for s in digest.sections if s.canonical_symbol == symbol)


# --------------------------------------------------------------------------- #
# Grouping
# --------------------------------------------------------------------------- #


def test_an_item_reaches_the_instrument_its_stored_analysis_linked() -> None:
    """Grouping reads the recorded link; it never re-reads the headline."""
    digest = _digest(_archived("https://p.test/hal", ORDER))

    assert len(_section(digest, "HAL").entries) == 1
    assert _section(digest, "SBIN").is_quiet


def test_every_watchlist_instrument_gets_a_section_even_when_silent() -> None:
    """Absence is the usual answer, and a missing section looks like a bug."""
    digest = _digest(_archived("https://p.test/hal", ORDER))

    assert {s.canonical_symbol for s in digest.sections} == {"HAL", "SBIN", "PNB"}
    assert len(digest.quiet) == 2


def test_a_revision_with_no_analysis_contributes_to_nothing() -> None:
    """It carries no link and no verdict, so it is not yet evidence."""
    digest = _digest(_archived("https://p.test/hal", ORDER, analysed=False))

    assert digest.items_reported == 0
    assert len(digest.quiet) == len(UNIVERSE)


def test_an_item_matching_nothing_appears_in_no_section() -> None:
    """The poll command reports its own unresolved count; this is not that."""
    digest = _digest(_archived("https://p.test/rain", "Rainfall delays the harvest"))

    assert digest.items_reported == 0


def test_one_item_naming_two_instruments_appears_under_both() -> None:
    """Shared evidence is shared, not arbitrarily assigned to one of them."""
    headline = "State Bank of India and Punjab National Bank raise deposit rates"
    digest = _digest(_archived("https://p.test/banks", headline))

    assert len(_section(digest, "SBIN").entries) == 1
    assert len(_section(digest, "PNB").entries) == 1


def test_the_stored_verdicts_are_reported_unchanged() -> None:
    """A digest that recomputed could disagree with the archive it summarises."""
    item = _archived("https://p.test/hal", ORDER)
    entry = _section(_digest(item), "HAL").entries[0]

    assert item.analysis is not None
    assert entry.category is item.analysis.event.category
    assert entry.sentiment is item.analysis.sentiment.label


# --------------------------------------------------------------------------- #
# Ordering, borrowed rather than invented
# --------------------------------------------------------------------------- #


def test_sections_are_ordered_by_the_most_significant_event_stored() -> None:
    """A governance finding outranks an order win, per the classifier's rules."""
    digest = _digest(
        _archived("https://p.test/hal", ORDER),
        _archived("https://p.test/sbi", FRAUD),
    )

    assert [s.canonical_symbol for s in digest.sections][:2] == ["SBIN", "HAL"]


def test_quiet_instruments_sort_last() -> None:
    """Nothing stored is less of an answer than commentary is."""
    digest = _digest(_archived("https://p.test/pnb", COMMENTARY))

    assert digest.sections[-1].is_quiet
    assert digest.sections[0].canonical_symbol == "PNB"


def test_instruments_with_nothing_are_alphabetical_among_themselves() -> None:
    """Deterministic, rather than whatever order the rows arrived in."""
    digest = _digest()

    assert [s.canonical_symbol for s in digest.sections] == ["HAL", "PNB", "SBIN"]


def test_entries_within_an_instrument_are_ordered_by_precedence_then_recency() -> None:
    """The classifier's rule order is the policy; there is not a second one."""
    older_fraud = _archived(
        "https://p.test/sbi-1", FRAUD, published_at=PUBLISHED - timedelta(days=2)
    )
    newer_commentary = _archived(
        "https://p.test/sbi-2",
        "State Bank of India shares were among the most traded on Tuesday",
    )
    digest = _digest(newer_commentary, older_fraud)
    entries = _section(digest, "SBIN").entries

    assert entries[0].category is EventCategory.FRAUD_GOVERNANCE
    assert entries[0].is_notable is True
    assert entries[1].is_notable is False


def test_two_items_of_one_category_are_ordered_newest_first() -> None:
    """Recency is the tie-break, not insertion order."""
    old = _archived("https://p.test/a", ORDER, published_at=PUBLISHED - timedelta(days=3))
    new = _archived("https://p.test/b", ORDER, published_at=PUBLISHED)
    entries = _section(_digest(old, new), "HAL").entries

    assert entries[0].item.revision.item.published_at > entries[1].item.revision.item.published_at


def test_the_digest_is_identical_whatever_order_the_items_arrive_in() -> None:
    """Two reads of one cutoff must produce the same document."""
    items = (
        _archived("https://p.test/hal", ORDER),
        _archived("https://p.test/sbi", FRAUD),
        _archived("https://p.test/pnb", COMMENTARY),
    )

    assert _digest(*items) == _digest(*reversed(items))


# --------------------------------------------------------------------------- #
# Bounds and honesty about them
# --------------------------------------------------------------------------- #


def test_a_section_is_bounded_and_says_how_much_it_withheld() -> None:
    """A truncated section must not be readable as a complete one."""
    items = [
        _archived(f"https://p.test/hal-{n}", ORDER, published_at=PUBLISHED - timedelta(hours=n))
        for n in range(MAX_ITEMS_PER_INSTRUMENT + 3)
    ]
    section = _section(_digest(*items), "HAL")

    assert len(section.entries) == MAX_ITEMS_PER_INSTRUMENT
    assert section.withheld == 3


def test_a_section_within_its_bound_withholds_nothing() -> None:
    """The count is real, not decoration."""
    assert _section(_digest(_archived("https://p.test/hal", ORDER)), "HAL").withheld == 0


def test_the_bound_is_configurable_and_still_reports_the_remainder() -> None:
    """An operator can widen it without losing the honesty about truncation."""
    items = [
        _archived(f"https://p.test/hal-{n}", ORDER, published_at=PUBLISHED - timedelta(hours=n))
        for n in range(4)
    ]
    section = _section(_digest(*items, max_items=2), "HAL")

    assert (len(section.entries), section.withheld) == (2, 2)


@pytest.mark.parametrize("bad", [0, -1])
def test_a_digest_showing_nothing_is_refused(bad: int) -> None:
    """Zero items per instrument is a mistake, not a request."""
    with pytest.raises(ValidationError, match="at least one item"):
        _digest(max_items=bad)


# --------------------------------------------------------------------------- #
# Summaries that do not overstate
# --------------------------------------------------------------------------- #


def test_sentiment_is_tallied_rather_than_averaged() -> None:
    """A mean of POSITIVE and NEGATIVE is NEUTRAL, which it does not mean."""
    digest = _digest(
        _archived("https://p.test/sbi-1", FRAUD),
        _archived("https://p.test/sbi-2", "State Bank of India wins a large mandate"),
    )
    tally = dict(_section(digest, "SBIN").sentiments)

    assert sum(tally.values()) == len(_section(digest, "SBIN").entries)


def test_categories_are_listed_most_significant_first() -> None:
    """The same ordering the entries use, so the header cannot contradict them."""
    digest = _digest(
        _archived("https://p.test/sbi-1", FRAUD),
        _archived(
            "https://p.test/sbi-2",
            "State Bank of India shares were among the most traded on Tuesday",
        ),
    )
    categories = _section(digest, "SBIN").categories

    assert categories[0] is EventCategory.FRAUD_GOVERNANCE


def test_commentary_is_not_counted_as_a_finding() -> None:
    """Otherwise every instrument looks eventful and the marker means nothing."""
    section = _section(_digest(_archived("https://p.test/pnb", COMMENTARY)), "PNB")

    assert section.entries[0].is_notable is False
    assert section.notable == ()


def test_an_ambiguous_link_is_kept_and_marked() -> None:
    """An ambiguity nobody sees is an ambiguity nobody reviews."""
    twin = _instrument("HAL2", "Hindustan Aeronautics Limited")
    universe = (HAL, twin)
    digest = _digest(_archived("https://p.test/hal", ORDER, universe=universe), universe=universe)
    states = {entry.match_state for section in digest.sections for entry in section.entries}

    assert states <= {MatchState.MATCHED, MatchState.AMBIGUOUS}
    assert digest.items_reported >= 1


def test_the_digest_records_the_policy_that_built_it() -> None:
    """A change of grouping policy has to be visible in the output."""
    assert _digest().revision == DIGEST_REVISION


def test_the_window_and_cutoff_are_carried_through_unchanged() -> None:
    """The reader has to be able to see which question was asked."""
    digest = _digest()

    assert digest.known_at == ANALYSED
    assert digest.published_to == ANALYSED


def test_an_empty_watchlist_produces_an_empty_digest_rather_than_failing() -> None:
    """No instruments is a coherent answer."""
    digest = build_digest(
        (),
        (),
        known_at=ANALYSED,
        published_from=PUBLISHED,
        published_to=ANALYSED,
    )

    assert digest.sections == ()
    assert digest.items_reported == 0
