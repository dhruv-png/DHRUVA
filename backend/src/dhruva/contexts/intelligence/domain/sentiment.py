"""Deterministic lexical sentiment baseline for financial headlines.

**This measures wording, not markets.** A negative label means the sentence is
phrased badly, not that the stock will fall. Nothing downstream may treat a
label here as a prediction, and sentiment alone may never create a trade -- it
adds context, warnings and ranking evidence to a decision made elsewhere.

Deterministic and versioned on purpose. The same text yields the same label,
score, confidence and matched terms in every process and every run, and the
input hash plus :data:`SENTIMENT_RULESET_REVISION` make a stored result
reproducible. That is also what lets this stay the fallback when an optional
local model is absent: a baseline that answers differently each time cannot be
the thing a missing model falls back to.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from dhruva.shared.invariants import invariant

__all__ = [
    "SENTIMENT_RULESET_REVISION",
    "AbstentionReason",
    "SentimentLabel",
    "SentimentResult",
    "evaluate_sentiment",
]

#: Ruleset identity recorded on every result. Changing a weight changes this.
SENTIMENT_RULESET_REVISION = "news-lexical-sentiment-v1"

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
_MIN_TOKENS = 3
_NEGATION_WINDOW = 4
_CENTS = Decimal("0.01")
_SHA256_HEX_LENGTH = 64

#: Weight above which one clause counts as carrying real polarity.
_CLAUSE_EVIDENCE = Decimal("1.0")
#: Score magnitude below which hedged language wins over weak polarity.
_UNCERTAIN_CEILING = Decimal("0.30")

_POSITIVE: dict[str, Decimal] = {
    "beats": Decimal("1.5"),
    "beat": Decimal("1.2"),
    "surges": Decimal("1.5"),
    "surge": Decimal("1.5"),
    "jumps": Decimal("1.2"),
    "jump": Decimal("1.2"),
    "rises": Decimal("1.0"),
    "rise": Decimal("1.0"),
    "rose": Decimal("1.0"),
    "gains": Decimal("1.0"),
    "gain": Decimal("1.0"),
    "growth": Decimal("1.0"),
    "record": Decimal("1.2"),
    "wins": Decimal("1.5"),
    "win": Decimal("1.5"),
    "won": Decimal("1.2"),
    "bags": Decimal("1.5"),
    "secures": Decimal("1.2"),
    "awarded": Decimal("1.2"),
    "upgrade": Decimal("1.5"),
    "upgrades": Decimal("1.5"),
    "dividend": Decimal("1.0"),
    "bonus": Decimal("1.0"),
    "approval": Decimal("1.0"),
    "approves": Decimal("1.0"),
    "cleared": Decimal("1.2"),
    "clears": Decimal("1.2"),
    "expansion": Decimal("0.8"),
    "strong": Decimal("1.0"),
    "improves": Decimal("1.0"),
    "eases": Decimal("0.8"),
    "appoints": Decimal("0.6"),
    "launches": Decimal("0.6"),
}

_NEGATIVE: dict[str, Decimal] = {
    "misses": Decimal("1.5"),
    "miss": Decimal("1.5"),
    "missed": Decimal("1.2"),
    "plunges": Decimal("1.5"),
    "plunge": Decimal("1.5"),
    "slumps": Decimal("1.5"),
    "falls": Decimal("1.0"),
    "fall": Decimal("1.0"),
    "fell": Decimal("1.0"),
    "declines": Decimal("1.0"),
    "decline": Decimal("1.0"),
    "drops": Decimal("1.0"),
    "drop": Decimal("1.0"),
    "loss": Decimal("1.2"),
    "losses": Decimal("1.2"),
    "weak": Decimal("1.0"),
    "weakens": Decimal("1.0"),
    "cut": Decimal("1.0"),
    "cuts": Decimal("1.0"),
    "downgrade": Decimal("1.5"),
    "downgrades": Decimal("1.5"),
    "delay": Decimal("0.8"),
    "delays": Decimal("0.8"),
    "halted": Decimal("1.2"),
    "shutdown": Decimal("1.2"),
    "strike": Decimal("1.0"),
    "recall": Decimal("1.2"),
    "resigns": Decimal("1.0"),
    "resignation": Decimal("1.0"),
    "default": Decimal("1.5"),
    "impairment": Decimal("1.2"),
    "warns": Decimal("1.2"),
    "warning": Decimal("1.0"),
}

#: Terms whose presence is the story. A governance or regulatory event is not
#: balanced out by a good quarter in the same sentence, so these outrank the
#: mixed rule instead of averaging against it.
_SEVERE: dict[str, Decimal] = {
    "fraud": Decimal("2.5"),
    "fraudulent": Decimal("2.5"),
    "probe": Decimal("2.0"),
    "investigation": Decimal("2.0"),
    "raid": Decimal("2.0"),
    "penalty": Decimal("2.0"),
    "penalises": Decimal("2.0"),
    "banned": Decimal("2.0"),
    "ban": Decimal("2.0"),
    "insolvency": Decimal("2.5"),
    "bankruptcy": Decimal("2.5"),
    "embezzlement": Decimal("2.5"),
    "misappropriation": Decimal("2.5"),
    "lawsuit": Decimal("2.0"),
    "sues": Decimal("2.0"),
}

_UNCERTAINTY = frozenset(
    {
        "may",
        "might",
        "could",
        "reportedly",
        "rumour",
        "rumours",
        "rumor",
        "rumors",
        "speculation",
        "speculated",
        "unconfirmed",
        "allegedly",
        "likely",
        "considering",
        "explores",
        "exploring",
        "denies",
        "denied",
        "talks",
        "mulls",
        "weighs",
    }
)

#: Negation inverts a term and halves it. "Cleared of fraud" is good news, but
#: not as good as the fraud was bad -- an absence of disaster is relief, not a
#: result. Halving says that in arithmetic instead of in a comment.
_NEGATORS = frozenset(
    {
        "not",
        "no",
        "never",
        "without",
        "fails",
        "failed",
        "denies",
        "denied",
        "dismisses",
        "dismissed",
        "withdraws",
        "withdrawn",
        "quashes",
        "quashed",
        "avoids",
        "avoided",
        "clears",
        "cleared",
        "acquitted",
        "exonerated",
        "absolved",
    }
)

_CONTRAST = ("but", "however", "despite", "though", "although", "yet", "while")


class SentimentLabel(StrEnum):
    """The five outcomes the baseline may report."""

    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"
    MIXED = "MIXED"
    UNCERTAIN = "UNCERTAIN"


class AbstentionReason(StrEnum):
    """Why the baseline declined to take a side."""

    INSUFFICIENT_TEXT = "INSUFFICIENT_TEXT"
    NO_LEXICAL_EVIDENCE = "NO_LEXICAL_EVIDENCE"
    HEDGED_LANGUAGE = "HEDGED_LANGUAGE"


@dataclass(frozen=True, slots=True)
class SentimentResult:
    """One reproducible verdict, its evidence and the ruleset that produced it."""

    label: SentimentLabel
    score: Decimal
    confidence: Decimal
    positive_terms: tuple[str, ...]
    negative_terms: tuple[str, ...]
    negated_terms: tuple[str, ...]
    contrast_present: bool
    uncertainty_present: bool
    abstention_reason: AbstentionReason | None
    input_sha256: str
    ruleset_revision: str = SENTIMENT_RULESET_REVISION

    def __post_init__(self) -> None:
        """Keep the score and confidence bounded and the abstention consistent."""
        invariant(Decimal(-1) <= self.score <= Decimal(1), "sentiment score must fall in [-1, 1]")
        invariant(
            Decimal(0) <= self.confidence <= Decimal(1),
            "sentiment confidence must fall in [0, 1]",
        )
        invariant(
            len(self.input_sha256) == _SHA256_HEX_LENGTH,
            "input hash must be a SHA-256 digest",
        )
        decided = {SentimentLabel.POSITIVE, SentimentLabel.NEGATIVE, SentimentLabel.MIXED}
        if self.label in decided:
            invariant(
                self.abstention_reason is None,
                "a decided label does not carry an abstention reason",
            )
        else:
            invariant(
                self.abstention_reason is not None,
                "a neutral or uncertain label must say why it took no side",
            )


@dataclass(frozen=True, slots=True)
class _Clause:
    """One contrast-delimited span and the polarity found inside it."""

    positive: Decimal
    negative: Decimal
    severe: Decimal
    positive_terms: tuple[str, ...]
    negative_terms: tuple[str, ...]
    negated_terms: tuple[str, ...]


def _normalise(value: str) -> str:
    """Fold text to the spacing and case the lexicons are written in."""
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", value.casefold())).strip()


def _split_clauses(tokens: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """Split on contrast words, which is where a headline changes its mind."""
    clauses: list[list[str]] = [[]]
    for token in tokens:
        if token in _CONTRAST:
            clauses.append([])
            continue
        clauses[-1].append(token)
    return tuple(tuple(item) for item in clauses if item)


def _score_clause(tokens: tuple[str, ...]) -> _Clause:
    """Accumulate one clause's polarity, applying negation within its window."""
    positive = Decimal(0)
    negative = Decimal(0)
    severe = Decimal(0)
    positive_terms: list[str] = []
    negative_terms: list[str] = []
    negated_terms: list[str] = []

    for index, token in enumerate(tokens):
        if token in _SEVERE:
            weight, polarity = _SEVERE[token], Decimal(-1)
        elif token in _NEGATIVE:
            weight, polarity = _NEGATIVE[token], Decimal(-1)
        elif token in _POSITIVE:
            weight, polarity = _POSITIVE[token], Decimal(1)
        else:
            continue

        window = tokens[max(0, index - _NEGATION_WINDOW) : index]
        negated = any(item in _NEGATORS for item in window)
        if negated:
            negated_terms.append(token)
            polarity, weight = -polarity, weight / 2

        if polarity > 0:
            positive += weight
            positive_terms.append(token)
        else:
            negative += weight
            negative_terms.append(token)
            if token in _SEVERE and not negated:
                severe += weight

    return _Clause(
        positive=positive,
        negative=negative,
        severe=severe,
        positive_terms=tuple(positive_terms),
        negative_terms=tuple(negative_terms),
        negated_terms=tuple(negated_terms),
    )


def _quantise(value: Decimal) -> Decimal:
    """Round a bounded score to two places, deterministically."""
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def evaluate_sentiment(text: str) -> SentimentResult:
    """Label a headline from its wording alone, deterministically.

    Precedence, strongest claim last: text too short to read, no polarity words
    at all, a governance or regulatory term that outranks anything positive
    beside it, real polarity on both sides, hedged language over weak polarity,
    and only then a plain direction.
    """
    digest = hashlib.sha256(text.encode()).hexdigest()
    folded = _normalise(text)
    tokens = tuple(folded.split())
    if len(tokens) < _MIN_TOKENS:
        return _abstained(
            SentimentLabel.UNCERTAIN,
            AbstentionReason.INSUFFICIENT_TEXT,
            digest,
            uncertainty_present=False,
            contrast_present=False,
        )

    contrast_present = any(item in _CONTRAST for item in tokens)
    uncertainty_present = any(item in _UNCERTAINTY for item in tokens)
    clauses = tuple(_score_clause(item) for item in _split_clauses(tokens))

    positive = sum((item.positive for item in clauses), Decimal(0))
    negative = sum((item.negative for item in clauses), Decimal(0))
    severe = sum((item.severe for item in clauses), Decimal(0))
    magnitude = positive + negative

    if magnitude == 0:
        # Hedging with nothing to hedge is still hedging. "May be considering a
        # stake sale" is not a neutral statement of fact, it is a report that
        # nobody will stand behind, and calling it NEUTRAL would let it rank
        # alongside a settled one.
        return _abstained(
            SentimentLabel.UNCERTAIN if uncertainty_present else SentimentLabel.NEUTRAL,
            AbstentionReason.HEDGED_LANGUAGE
            if uncertainty_present
            else AbstentionReason.NO_LEXICAL_EVIDENCE,
            digest,
            uncertainty_present=uncertainty_present,
            contrast_present=contrast_present,
        )

    evidence = _Clause(
        positive=positive,
        negative=negative,
        severe=severe,
        positive_terms=tuple(term for item in clauses for term in item.positive_terms),
        negative_terms=tuple(term for item in clauses for term in item.negative_terms),
        negated_terms=tuple(term for item in clauses for term in item.negated_terms),
    )
    score = _quantise((positive - negative) / magnitude)
    confidence = _quantise(min(Decimal(1), magnitude / Decimal(4)))
    if uncertainty_present:
        confidence = _quantise(max(Decimal(0), confidence - Decimal("0.25")))

    label, abstention = _decide(
        evidence,
        score=score,
        uncertainty_present=uncertainty_present,
        contrast_present=contrast_present,
    )
    return SentimentResult(
        label=label,
        score=score,
        confidence=confidence,
        positive_terms=evidence.positive_terms,
        negative_terms=evidence.negative_terms,
        negated_terms=evidence.negated_terms,
        contrast_present=contrast_present,
        uncertainty_present=uncertainty_present,
        abstention_reason=abstention,
        input_sha256=digest,
    )


def _decide(
    evidence: _Clause,
    *,
    score: Decimal,
    uncertainty_present: bool,
    contrast_present: bool,
) -> tuple[SentimentLabel, AbstentionReason | None]:
    """Apply the label precedence to one headline's accumulated evidence."""
    positive, negative, severe = evidence.positive, evidence.negative, evidence.severe
    if severe > 0:
        return SentimentLabel.NEGATIVE, None
    both_sides = positive >= _CLAUSE_EVIDENCE and negative >= _CLAUSE_EVIDENCE
    if both_sides or (contrast_present and positive > 0 and negative > 0):
        return SentimentLabel.MIXED, None
    if uncertainty_present and abs(score) < _UNCERTAIN_CEILING:
        return SentimentLabel.UNCERTAIN, AbstentionReason.HEDGED_LANGUAGE
    if score > 0:
        return SentimentLabel.POSITIVE, None
    if score < 0:
        return SentimentLabel.NEGATIVE, None
    return SentimentLabel.NEUTRAL, AbstentionReason.NO_LEXICAL_EVIDENCE


def _abstained(
    label: SentimentLabel,
    reason: AbstentionReason,
    digest: str,
    *,
    uncertainty_present: bool,
    contrast_present: bool,
) -> SentimentResult:
    """Build a result that takes no side and says why."""
    return SentimentResult(
        label=label,
        score=Decimal("0.00"),
        confidence=Decimal("0.00"),
        positive_terms=(),
        negative_terms=(),
        negated_terms=(),
        contrast_present=contrast_present,
        uncertainty_present=uncertainty_present,
        abstention_reason=reason,
        input_sha256=digest,
    )
