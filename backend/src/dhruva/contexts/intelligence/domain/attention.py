"""The pure vocabulary of research attention: points, bands and the verdict type.

Answers one question: "given information DHRUVA already has, which watchlist
instruments have the most noteworthy *observable* context right now?" That is
the whole scope. This module computes nothing new about the future, and
nothing here may be read as which instrument is a better investment --
"attention" means *more change or more written-about right now*, never
*more likely to rise*, *more expected to perform*, or *more attractive*.

Every contributing fact is a magnitude of something that already happened: a
stored price move, a stored volume ratio, a count of already-archived,
already-classified news items. Nothing is fetched, nothing is predicted, and
nothing is recomputed from a headline -- the event category and sentiment used
here are read exactly as :mod:`dhruva.contexts.intelligence.domain.digest`
already reports them, for the identical reason that module gives: two derived
readings of one fact eventually disagree, and there would be no way to tell
which one was wrong.

**Points, not a formula.** Each of four observable conditions independently
contributes zero or a small bounded number of points; the total is the score,
and the score maps to one of four bands by a fixed threshold. There is no
multiplication of dissimilar units, no fitted weight and no continuous curve
standing in for a model this codebase does not have and should not pretend to.
The thresholds below are a conservative, documented starting point -- not a
calibrated finding -- and are exactly as arbitrary as they look; the
determinism and the explanation are the guarantee, not the specific numbers.

**Symmetric by construction.** A move is scored by its magnitude
(``abs(percent)``), so a 5% fall and a 5% rise draw exactly the same
attention. A band that treated a rise as more attention-worthy than an equal
fall would be a directional opinion wearing an objectivity costume.

**Absence is never zero.** An instrument DHRUVA has no market data for scores
no price or volume points and says so explicitly
(:attr:`ResearchAttention.market_context_available`); it does not silently
read the same as an instrument that is flat and unchanged. The point-in-time
rule that governs everything else in this codebase governs this module too --
this file adds none of its own; the composition that reads a ``MarketContext``
and produces a :class:`ResearchAttention` lives in
:mod:`dhruva.contexts.intelligence.interfaces.attention_presentation`
(:func:`~dhruva.contexts.intelligence.interfaces.attention_presentation.rank_watchlist`),
never here -- the intelligence domain reaches no other context (ADR-001,
enforced by ``test_domain_purity.py``), for the same reason
:mod:`dhruva.contexts.intelligence.domain.digest` never imports
``MarketContext`` either.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import datetime

    from dhruva.shared.identity import InstrumentId

__all__ = [
    "ATTENTION_REVISION",
    "MAX_ATTENTION_SCORE",
    "NEWS_ITEM_POINT_CAP",
    "NOTABLE_EVENT_BONUS",
    "AttentionBand",
    "ResearchAttention",
    "band_for_score",
    "move_points",
    "volume_points",
]

#: Ruleset identity recorded on every result, so a future change of weights or
#: thresholds is visible in an operator's output rather than only in this
#: file's history.
ATTENTION_REVISION = "watchlist-attention-v1"

# --------------------------------------------------------------------------- #
# Bounded, documented, arbitrary-and-says-so thresholds
# --------------------------------------------------------------------------- #

#: A move smaller than this is not worth a reason line at all -- most sessions
#: move a little, and reporting every fraction of a percent would bury the
#: sessions that actually moved.
_MOVE_NOTEWORTHY_PERCENT = Decimal("2")
_MOVE_LARGE_PERCENT = Decimal("5")
_MOVE_VERY_LARGE_PERCENT = Decimal("8")
#: Points for a move at or above each threshold above, lowest threshold first.
_MOVE_POINTS = (1, 2, 3)

#: Volume ratio below this is unremarkable; only *excess* over the recent
#: baseline is treated as an attention factor, not volume below it.
_VOLUME_NOTEWORTHY_RATIO = Decimal("1.25")
_VOLUME_HIGH_RATIO = Decimal("2")
_VOLUME_VERY_HIGH_RATIO = Decimal("3")
_VOLUME_POINTS = (1, 2, 3)

#: One point per archived item in the digest window, capped so a single very
#: newsy instrument cannot dominate the ranking on count alone. Public because
#: the composition that counts a section's items lives outside this module
#: (see the module docstring).
NEWS_ITEM_POINT_CAP = 3

#: Extra points when the most significant category present is a specific kind
#: of event rather than commentary or an unclassifiable headline -- reusing
#: exactly the classifier's own notion of "notable"
#: (:attr:`~dhruva.contexts.intelligence.domain.digest.DigestEntry.is_notable`),
#: never a second opinion about which categories matter.
NOTABLE_EVENT_BONUS = 2

#: The greatest score every component above can jointly produce: one 1-day
#: move (3) + one multi-day move (3) + volume (3) + news count (3) + the
#: notable-event bonus (2). Used only to bound :class:`ResearchAttention` and
#: to keep this docstring and the arithmetic from silently drifting apart.
MAX_ATTENTION_SCORE = 3 + 3 + 3 + NEWS_ITEM_POINT_CAP + NOTABLE_EVENT_BONUS

#: Score at or above which a band applies, highest first. A score below every
#: threshold here is LOW.
_HIGH_SCORE = 7
_ELEVATED_SCORE = 3
_NORMAL_SCORE = 1


class AttentionBand(StrEnum):
    """A coarse, reader-facing summary of the score.

    Observable change and written-about-ness, never desirability.
    """

    #: Nothing observed crossed any noteworthy threshold.
    LOW = "LOW"
    #: At least one observable condition was mildly noteworthy.
    NORMAL = "NORMAL"
    #: Multiple observable conditions were noteworthy, or one was pronounced.
    ELEVATED = "ELEVATED"
    #: Several observable conditions were pronounced at once.
    HIGH = "HIGH"


def band_for_score(score: int) -> AttentionBand:
    """Map a score to its band by the fixed thresholds documented above."""
    if score >= _HIGH_SCORE:
        return AttentionBand.HIGH
    if score >= _ELEVATED_SCORE:
        return AttentionBand.ELEVATED
    if score >= _NORMAL_SCORE:
        return AttentionBand.NORMAL
    return AttentionBand.LOW


@dataclass(frozen=True, slots=True)
class ResearchAttention:
    """One instrument's deterministic research-attention verdict at a cutoff.

    Never a view on value. ``score`` and ``band`` describe how much observable
    change or archived coverage exists right now, and ``reasons`` names every
    fact that contributed, in the exact wording a reader can check against the
    digest itself.
    """

    instrument_id: InstrumentId
    canonical_symbol: str
    company_name: str
    as_of: datetime
    score: int
    band: AttentionBand
    #: Every contributing observable fact, mechanically stated -- never advice
    #: language. Empty exactly when the score is zero and nothing else was
    #: worth naming.
    reasons: tuple[str, ...]
    #: ``False`` means DHRUVA has no usable market data for this instrument at
    #: this cutoff, not that nothing moved. Distinct from an empty ``reasons``
    #: entry so a caller cannot mistake "we do not know" for "unchanged".
    market_context_available: bool
    revision: str = ATTENTION_REVISION

    def __post_init__(self) -> None:
        """Require a score that is bounded and a band that agrees with it."""
        invariant(0 <= self.score <= MAX_ATTENTION_SCORE, "attention score is out of bounds")
        invariant(
            self.band is band_for_score(self.score),
            "attention band does not match its own score",
        )

    @property
    def sort_key(self) -> tuple[int, str]:
        """Rank by score, highest first, then alphabetically for a deterministic tie."""
        return (-self.score, self.canonical_symbol)


def move_points(percent: Decimal, *, label: str) -> tuple[int, str | None]:
    """Return the points and reason for one movement, symmetric in sign."""
    magnitude = abs(percent)
    if magnitude < _MOVE_NOTEWORTHY_PERCENT:
        return 0, None
    if magnitude >= _MOVE_VERY_LARGE_PERCENT:
        points = _MOVE_POINTS[2]
    elif magnitude >= _MOVE_LARGE_PERCENT:
        points = _MOVE_POINTS[1]
    else:
        points = _MOVE_POINTS[0]
    return points, f"{label} absolute move {magnitude}%"


def volume_points(ratio: Decimal, baseline_sessions: int) -> tuple[int, str | None]:
    """Return the points and reason for volume, counting only excess over normal."""
    if ratio < _VOLUME_NOTEWORTHY_RATIO:
        return 0, None
    if ratio >= _VOLUME_VERY_HIGH_RATIO:
        points = _VOLUME_POINTS[2]
    elif ratio >= _VOLUME_HIGH_RATIO:
        points = _VOLUME_POINTS[1]
    else:
        points = _VOLUME_POINTS[0]
    return points, f"volume {ratio}x prior-{baseline_sessions}-session mean"
