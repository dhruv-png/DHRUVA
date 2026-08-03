"""The deterministic lexical sentiment baseline, label by label."""

from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from dhruva.contexts.intelligence.domain.sentiment import (
    SENTIMENT_RULESET_REVISION,
    AbstentionReason,
    SentimentLabel,
    evaluate_sentiment,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("headline", "expected"),
    [
        ("Adani Ports Q1 net profit rises 18% to a record high", SentimentLabel.POSITIVE),
        ("HAL bags an order worth Rs 5,000 crore from the ministry", SentimentLabel.POSITIVE),
        ("Company board approves an interim dividend of Rs 4 per share", SentimentLabel.POSITIVE),
        ("SBI shares plunge as net profit falls 12%", SentimentLabel.NEGATIVE),
        ("Chief executive resigns with immediate effect", SentimentLabel.NEGATIVE),
        ("Nifty 50 ends flat in a quiet session", SentimentLabel.NEUTRAL),
        ("Board meeting scheduled for Tuesday", SentimentLabel.NEUTRAL),
    ],
)
def test_plain_headlines_get_the_obvious_label(headline: str, expected: SentimentLabel) -> None:
    """The baseline has to get the easy cases right before anything else matters."""
    assert evaluate_sentiment(headline).label is expected


def test_a_governance_term_outranks_anything_positive_beside_it() -> None:
    """A regulator's penalty is not balanced out by a good quarter in the same line."""
    result = evaluate_sentiment("Regulator imposes penalty despite record revenue growth")

    assert result.label is SentimentLabel.NEGATIVE
    assert "penalty" in result.negative_terms
    assert result.score > 0


def test_an_earnings_beat_with_weak_guidance_is_mixed() -> None:
    """Both halves are true, and collapsing them to one direction loses the story."""
    result = evaluate_sentiment("Profit beats estimates but the company cuts guidance for FY27")

    assert result.label is SentimentLabel.MIXED
    assert result.contrast_present
    assert "beats" in result.positive_terms
    assert "cuts" in result.negative_terms
    assert result.abstention_reason is None


def test_a_contrast_clause_splits_a_headline_that_changes_its_mind() -> None:
    """The word "despite" is where the sentence turns, and both sides are kept."""
    result = evaluate_sentiment("Despite strong revenue growth the company reported a loss")

    assert result.label is SentimentLabel.MIXED
    assert result.contrast_present


def test_negation_inverts_a_term_and_halves_it() -> None:
    """An absence of good news is mild bad news, not a strong claim either way."""
    result = evaluate_sentiment("Profit did not rise this quarter")

    assert result.label is SentimentLabel.NEGATIVE
    assert result.negated_terms == ("rise",)


def test_negation_can_lift_a_governance_term_off_a_headline() -> None:
    """Being cleared of fraud is relief; the severity rule must not fire on it."""
    result = evaluate_sentiment("Tribunal clears the company of fraud allegations")

    assert result.label is SentimentLabel.POSITIVE
    assert "fraud" in result.negated_terms


def test_hedged_language_with_weak_polarity_is_uncertain() -> None:
    """A report nobody will stand behind must not rank beside a settled one."""
    result = evaluate_sentiment("Company may be considering a stake sale, sources say")

    assert result.label is SentimentLabel.UNCERTAIN
    assert result.uncertainty_present
    assert result.abstention_reason is AbstentionReason.HEDGED_LANGUAGE


def test_hedging_lowers_confidence_without_hiding_the_evidence() -> None:
    """A hedged positive is still positive; it is just believed less."""
    hedged = evaluate_sentiment("Company reportedly wins a small order")
    plain = evaluate_sentiment("Company wins a small order")

    assert hedged.label is SentimentLabel.POSITIVE
    assert hedged.uncertainty_present
    assert hedged.confidence < plain.confidence


@pytest.mark.parametrize("headline", ["", "ok", "Q1 out"])
def test_text_too_short_to_read_abstains(headline: str) -> None:
    """Declining is honest; a label on two words would be noise with a name."""
    result = evaluate_sentiment(headline)

    assert result.label is SentimentLabel.UNCERTAIN
    assert result.abstention_reason is AbstentionReason.INSUFFICIENT_TEXT
    assert result.confidence == Decimal("0.00")
    assert result.score == Decimal("0.00")


def test_a_readable_headline_with_no_polarity_words_is_neutral() -> None:
    """Neutral is a finding about the wording, and it says which finding."""
    result = evaluate_sentiment("Board meeting scheduled for Tuesday")

    assert result.label is SentimentLabel.NEUTRAL
    assert result.abstention_reason is AbstentionReason.NO_LEXICAL_EVIDENCE
    assert result.positive_terms == ()
    assert result.negative_terms == ()


def test_a_macro_headline_is_read_from_its_words_like_any_other() -> None:
    """The baseline has no special macro model, and does not pretend to."""
    assert evaluate_sentiment("Inflation eases to 4.1% in July").label is SentimentLabel.POSITIVE
    assert (
        evaluate_sentiment("Repo rate left unchanged at the policy meeting").label
        is SentimentLabel.NEUTRAL
    )


@pytest.mark.parametrize(
    "headline",
    [
        "Adani Ports Q1 net profit rises 18% to a record high",
        "SBI shares plunge as net profit falls 12%",
        "Profit beats estimates but the company cuts guidance for FY27",
        "Company may be considering a stake sale, sources say",
        "",
    ],
)
def test_score_and_confidence_stay_inside_their_bounds(headline: str) -> None:
    """An unbounded score cannot be compared, ranked or stored honestly."""
    result = evaluate_sentiment(headline)

    assert Decimal(-1) <= result.score <= Decimal(1)
    assert Decimal(0) <= result.confidence <= Decimal(1)


def test_confidence_grows_with_the_weight_of_evidence() -> None:
    """One weak word should not be believed as much as several strong ones."""
    weak = evaluate_sentiment("Chief executive resigns with immediate effect")
    strong = evaluate_sentiment("Company wins a record order and beats profit estimates")

    assert strong.confidence > weak.confidence


def test_a_decided_label_never_carries_an_abstention_reason() -> None:
    """Abstention and a verdict are different answers and cannot both be given."""
    for headline in (
        "HAL bags an order worth Rs 5,000 crore",
        "SBI shares plunge as net profit falls 12%",
        "Profit beats estimates but the company cuts guidance",
    ):
        assert evaluate_sentiment(headline).abstention_reason is None


def test_every_result_carries_its_input_hash_and_ruleset_revision() -> None:
    """A stored result produced by an older ruleset must be visibly older."""
    headline = "HAL bags an order worth Rs 5,000 crore"
    result = evaluate_sentiment(headline)

    assert result.ruleset_revision == SENTIMENT_RULESET_REVISION
    assert result.input_sha256 == hashlib.sha256(headline.encode()).hexdigest()


def test_the_input_hash_distinguishes_texts_that_normalise_alike() -> None:
    """The hash identifies the exact input, not the folded form the rules read."""
    first = evaluate_sentiment("HAL bags an order worth Rs 5,000 crore")
    second = evaluate_sentiment("hal bags an order worth rs 5,000 crore!")

    assert first.label is second.label
    assert first.input_sha256 != second.input_sha256


def test_evaluation_is_deterministic() -> None:
    """The fallback for a missing model cannot answer differently each time."""
    headline = "Profit beats estimates but the company cuts guidance for FY27"

    assert evaluate_sentiment(headline) == evaluate_sentiment(headline)


def test_the_five_required_labels_are_all_reachable() -> None:
    """Every label the product promises must be something the baseline can produce."""
    produced = {
        evaluate_sentiment(headline).label
        for headline in (
            "HAL bags an order worth Rs 5,000 crore from the ministry",
            "SBI shares plunge as net profit falls 12%",
            "Board meeting scheduled for Tuesday",
            "Profit beats estimates but the company cuts guidance for FY27",
            "Company may be considering a stake sale, sources say",
        )
    }

    assert produced == set(SentimentLabel)
