"""Every bounded event category, and the ordering that decides between them."""

from __future__ import annotations

import pytest

from dhruva.contexts.intelligence.domain.events import (
    EVENT_CLASSIFICATION_REVISION,
    EventCategory,
    classify_event,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Company reports Q1 results with net profit up 18%", EventCategory.EARNINGS_RESULTS),
        ("Company raises outlook for FY27", EventCategory.GUIDANCE),
        ("HAL bags order worth Rs 5,000 crore", EventCategory.ORDER_WIN),
        ("Firm acquires rival in all-cash deal", EventCategory.MERGER_ACQUISITION),
        ("Board approves QIP to raise capital", EventCategory.CAPITAL_RAISING),
        ("Board declares interim dividend", EventCategory.DIVIDEND),
        ("Company announces bonus issue in 1:1 ratio", EventCategory.SPLIT_BONUS),
        ("SEBI issues show cause notice to the company", EventCategory.REGULATORY_ACTION),
        ("Company faces a lawsuit in the high court", EventCategory.LITIGATION),
        ("Forensic audit finds accounting irregularities", EventCategory.FRAUD_GOVERNANCE),
        ("Board appoints new chief executive", EventCategory.MANAGEMENT_CHANGE),
        ("CRISIL revises credit rating outlook", EventCategory.RATING_ACTION),
        ("Automaker launches a new electric model", EventCategory.PRODUCT_LAUNCH),
        ("Fire at the plant halts production", EventCategory.OPERATIONAL_DISRUPTION),
        ("Inflation eases as repo rate stays unchanged", EventCategory.MACROECONOMIC),
        ("Banking sector sees improved asset quality", EventCategory.SECTOR_WIDE),
        ("Markets drift sideways ahead of the weekend", EventCategory.GENERAL_COMMENTARY),
        ("Update", EventCategory.UNKNOWN),
    ],
)
def test_each_bounded_category_is_reachable(headline: str, expected: EventCategory) -> None:
    """A category nothing can reach is decoration, not a taxonomy."""
    assert classify_event(headline).category is expected


def test_the_category_set_is_fixed_and_complete() -> None:
    """Adding a category is a versioned decision, so the set is pinned here."""
    assert len(set(EventCategory)) == 18


def test_consequence_outranks_wording_when_two_rules_could_fire() -> None:
    """A results headline mentioning a regulator is a regulatory event."""
    result = classify_event("SEBI probe follows the company's Q1 results")

    assert result.category is EventCategory.REGULATORY_ACTION
    assert result.matched_phrase == "sebi"


def test_governance_outranks_every_other_rule() -> None:
    """Nothing in the ordering may come before a governance finding."""
    result = classify_event("Record profit reported even as fraud allegations surface")

    assert result.category is EventCategory.FRAUD_GOVERNANCE


def test_a_specific_category_names_the_phrase_that_selected_it() -> None:
    """A classification nobody can check is a classification nobody should trust."""
    result = classify_event("Board declares interim dividend")

    assert result.matched_phrase == "dividend"
    assert "dividend" in result.reason
    assert result.revision == EVENT_CLASSIFICATION_REVISION


def test_undecided_categories_carry_no_phrase() -> None:
    """Commentary and unknown are answers about absence, not about a match."""
    assert classify_event("Markets drift sideways").matched_phrase is None
    assert classify_event("Update").matched_phrase is None


def test_text_too_short_to_read_is_unknown_rather_than_commentary() -> None:
    """Declining is honest; defaulting would later read as a finding."""
    assert classify_event("").category is EventCategory.UNKNOWN
    assert classify_event("Q1").category is EventCategory.UNKNOWN
    assert "too short" in classify_event("").reason


def test_a_phrase_inside_a_longer_word_does_not_match() -> None:
    """Substring matching is how "banned" appears inside an unrelated word."""
    assert classify_event("Urban development plans announced today").category is (
        EventCategory.GENERAL_COMMENTARY
    )


def test_classification_is_deterministic() -> None:
    """The same headline classifies identically in every process and every run."""
    assert classify_event("Board declares interim dividend") == classify_event(
        "Board declares interim dividend"
    )


def test_classification_is_independent_of_case_and_punctuation() -> None:
    """A shouted wire headline is the same event as the site that reprinted it."""
    assert (
        classify_event("BOARD DECLARES INTERIM DIVIDEND!").category
        is classify_event("Board declares interim dividend").category
    )
