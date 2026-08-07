"""Bounded event categories and the deterministic rules that assign one.

What happened and whether it sounds good are different questions, answered by
different modules on purpose. A fraud investigation and a record profit are both
strongly-worded; only one of them is a governance event, and a classifier that
learned "strongly worded" would confuse them forever.

The rules are ordered by consequence, not by keyword count. A headline that
mentions both a results date and a SEBI investigation is a regulatory event that
happens to mention results, and reading it the other way round is the failure
this ordering exists to prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from dhruva.shared.invariants import invariant

__all__ = [
    "EVENT_CLASSIFICATION_REVISION",
    "EventCategory",
    "EventClassification",
    "classify_event",
    "event_precedence",
]

#: Ruleset identity recorded on every classification.
EVENT_CLASSIFICATION_REVISION = "news-event-category-v1"

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
#: Fewest words before a headline is worth classifying at all.
_MIN_WORDS = 2


class EventCategory(StrEnum):
    """The fixed set of corporate and market events DHRUVA distinguishes."""

    EARNINGS_RESULTS = "EARNINGS_RESULTS"
    GUIDANCE = "GUIDANCE"
    ORDER_WIN = "ORDER_WIN"
    MERGER_ACQUISITION = "MERGER_ACQUISITION"
    CAPITAL_RAISING = "CAPITAL_RAISING"
    DIVIDEND = "DIVIDEND"
    SPLIT_BONUS = "SPLIT_BONUS"
    REGULATORY_ACTION = "REGULATORY_ACTION"
    LITIGATION = "LITIGATION"
    FRAUD_GOVERNANCE = "FRAUD_GOVERNANCE"
    MANAGEMENT_CHANGE = "MANAGEMENT_CHANGE"
    RATING_ACTION = "RATING_ACTION"
    PRODUCT_LAUNCH = "PRODUCT_LAUNCH"
    OPERATIONAL_DISRUPTION = "OPERATIONAL_DISRUPTION"
    MACROECONOMIC = "MACROECONOMIC"
    SECTOR_WIDE = "SECTOR_WIDE"
    GENERAL_COMMENTARY = "GENERAL_COMMENTARY"
    UNKNOWN = "UNKNOWN"


#: Ordered rules. The first category with a matching phrase wins, so the order
#: is the policy: what a reader must not miss comes before what merely describes.
_RULES: tuple[tuple[EventCategory, tuple[str, ...]], ...] = (
    (
        EventCategory.FRAUD_GOVERNANCE,
        (
            "fraud",
            "forensic audit",
            "accounting irregularities",
            "whistleblower",
            "misappropriation",
            "insider trading",
            "governance lapse",
            "auditor resigns",
            "shell companies",
        ),
    ),
    (
        EventCategory.REGULATORY_ACTION,
        (
            "sebi",
            "rbi imposes",
            "regulator",
            "regulatory",
            "show cause notice",
            "penalty",
            "fine of",
            "licence cancelled",
            "license cancelled",
            "ban on",
            "banned",
            "probe",
            "investigation",
            "raid",
            "search and seizure",
            "compliance breach",
        ),
    ),
    (
        EventCategory.LITIGATION,
        (
            "lawsuit",
            "sues",
            "sued",
            "court",
            "tribunal",
            "nclt",
            "arbitration",
            "verdict",
            "appeal",
            "petition",
            "insolvency",
        ),
    ),
    (
        EventCategory.RATING_ACTION,
        (
            "credit rating",
            "rating upgrade",
            "rating downgrade",
            "downgrades rating",
            "upgrades rating",
            "outlook revised",
            "crisil",
            "icra",
            "care ratings",
            "moody",
            "fitch",
            "s&p global ratings",
        ),
    ),
    (
        EventCategory.MERGER_ACQUISITION,
        (
            "acquire",
            "acquires",
            "acquisition",
            "merger",
            "merge with",
            "takeover",
            "stake sale",
            "buys stake",
            "divest",
            "demerger",
        ),
    ),
    (
        EventCategory.CAPITAL_RAISING,
        (
            "qip",
            "rights issue",
            "fund raise",
            "fundraise",
            "raise funds",
            "raises funds",
            "preferential allotment",
            "issue of bonds",
            "ncd issue",
            "ipo",
            "follow on public offer",
        ),
    ),
    (
        EventCategory.SPLIT_BONUS,
        ("stock split", "share split", "bonus issue", "bonus shares", "face value split"),
    ),
    (
        EventCategory.DIVIDEND,
        ("dividend", "interim dividend", "final dividend", "record date for dividend"),
    ),
    (
        EventCategory.ORDER_WIN,
        (
            "order win",
            "wins order",
            "bags order",
            "bags contract",
            "wins contract",
            "secures contract",
            "letter of intent",
            "order worth",
            "contract worth",
            "awarded a contract",
        ),
    ),
    (
        EventCategory.GUIDANCE,
        (
            "guidance",
            "outlook for fy",
            "cuts outlook",
            "raises outlook",
            "expects revenue",
            "targets revenue",
            "margin outlook",
        ),
    ),
    (
        EventCategory.EARNINGS_RESULTS,
        (
            "results",
            "quarterly earnings",
            "earnings",
            "net profit",
            "profit rises",
            "profit falls",
            "revenue rises",
            "revenue falls",
            "ebitda",
            "q1",
            "q2",
            "q3",
            "q4",
        ),
    ),
    (
        EventCategory.MANAGEMENT_CHANGE,
        (
            "appoints",
            "steps down",
            "resigns",
            "resignation",
            "new ceo",
            "new chairman",
            "managing director",
            "board approves appointment",
            "succeeds as",
        ),
    ),
    (
        EventCategory.OPERATIONAL_DISRUPTION,
        (
            "plant shutdown",
            "production halted",
            "fire at",
            "strike at",
            "workers strike",
            "recall",
            "outage",
            "supply disruption",
            "accident at",
            "flight cancellations",
        ),
    ),
    (
        EventCategory.PRODUCT_LAUNCH,
        ("launches", "unveils", "new model", "rolls out", "introduces", "debuts"),
    ),
    (
        EventCategory.MACROECONOMIC,
        (
            "inflation",
            "gdp",
            "repo rate",
            "monetary policy",
            "rbi policy",
            "trade deficit",
            "fiscal deficit",
            "crude oil prices",
            "rupee weakens",
            "rupee strengthens",
            "us fed",
        ),
    ),
    (
        EventCategory.SECTOR_WIDE,
        (
            "sector",
            "banks across",
            "auto industry",
            "airlines industry",
            "shipbuilding industry",
            "industry wide",
            "peers",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class EventClassification:
    """One category, the phrase that chose it and the ruleset that applied."""

    category: EventCategory
    matched_phrase: str | None
    reason: str
    revision: str = EVENT_CLASSIFICATION_REVISION

    def __post_init__(self) -> None:
        """Require a decided category to name the evidence that decided it."""
        invariant(bool(self.reason), "an event classification must give a reason")
        invariant(
            (self.matched_phrase is None)
            == (self.category in {EventCategory.UNKNOWN, EventCategory.GENERAL_COMMENTARY}),
            "a specific category names the phrase that selected it",
        )


def _normalise(value: str) -> str:
    """Fold text to the spacing and case the phrase rules are written in."""
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", value.casefold())).strip()


def classify_event(text: str) -> EventClassification:
    """Assign exactly one bounded category to a headline, or admit it cannot.

    Empty or unusably short text is ``UNKNOWN``: the classifier declines rather
    than defaulting to a category that would later read as a finding. Readable
    text that matches no rule is ``GENERAL_COMMENTARY``, which is a real answer
    -- most market copy is commentary.
    """
    folded = _normalise(text)
    if len(folded.split()) < _MIN_WORDS:
        return EventClassification(
            category=EventCategory.UNKNOWN,
            matched_phrase=None,
            reason="text is too short to classify",
        )
    padded = f" {folded} "
    for category, phrases in _RULES:
        for phrase in phrases:
            if f" {phrase} " in padded:
                return EventClassification(
                    category=category,
                    matched_phrase=phrase,
                    reason=f"matched the bounded phrase '{phrase}'",
                )
    return EventClassification(
        category=EventCategory.GENERAL_COMMENTARY,
        matched_phrase=None,
        reason="no bounded event phrase matched",
    )


def event_precedence(category: EventCategory) -> int:
    """Return how early this category is considered, lowest first.

    The rule order above is already a statement about what matters: "what a
    reader must not miss comes before what merely describes". Anything that
    needs to rank events by importance reads that order rather than inventing a
    second one, so a change of policy happens in one place and cannot leave two
    parts of the system disagreeing about whether fraud outranks a product
    launch.

    Categories that no rule produces sort last, which is correct for both of
    them: ``GENERAL_COMMENTARY`` is what the classifier says when nothing
    specific matched, and ``UNKNOWN`` is what it says when it declined to look.
    """
    for position, (candidate, _) in enumerate(_RULES):
        if candidate is category:
            return position
    return len(_RULES)
