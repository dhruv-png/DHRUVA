"""Group archived evidence into one section per instrument, as of one instant.

This answers "what changed for the things I follow?" from facts that are already
stored. It computes nothing new: every category, sentiment verdict and instrument
link it reports was decided by a ruleset at ingestion time and written down.
Re-deriving any of them here would let a digest disagree with the archive it
claims to summarise.

Two orderings, both borrowed rather than invented.

**Within an instrument**, items sort by the event classifier's own rule order --
"what a reader must not miss comes before what merely describes" -- then by
recency. A second importance scale living here would eventually disagree with
the first, and there is no way to tell which one is wrong.

**Between instruments**, sections sort by the highest-precedence event each one
carries, then by canonical symbol. So an instrument with a governance finding is
above one with a product launch, and two instruments with nothing but commentary
are in alphabetical order rather than in whatever order the rows arrived.

An instrument with no evidence still gets a section. Absence is the answer to
"what changed?" more often than not, and a digest that silently omitted quiet
instruments would be indistinguishable from one that lost them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.domain.entity_linking import MatchState
from dhruva.contexts.intelligence.domain.events import EventCategory, event_precedence
from dhruva.contexts.intelligence.domain.sentiment import SentimentLabel
from dhruva.shared.errors import ValidationError
from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from dhruva.contexts.intelligence.domain.archive import ArchivedNewsItem
    from dhruva.contexts.intelligence.domain.entity_linking import LinkableInstrument
    from dhruva.shared.identity import InstrumentId

__all__ = [
    "DIGEST_REVISION",
    "MAX_ITEMS_PER_INSTRUMENT",
    "DigestEntry",
    "DigestSection",
    "WatchlistDigest",
    "build_digest",
]

#: Recorded on every digest, so a change of grouping policy is visible in an
#: operator's output rather than only in this file's history.
DIGEST_REVISION = "watchlist-digest-v1"

#: Items shown per instrument before the section says how many more there are.
#: A digest is something a person reads over a coffee; past this it becomes a
#: list nobody scans, which is the same as no digest at all.
MAX_ITEMS_PER_INSTRUMENT = 5

#: Categories the classifier produces when nothing specific matched, or when it
#: declined to look. Neither is a finding, and counting them as one would make
#: every instrument look eventful.
_UNREMARKABLE = frozenset({EventCategory.GENERAL_COMMENTARY, EventCategory.UNKNOWN})


@dataclass(frozen=True, slots=True)
class DigestEntry:
    """One archived item as it appears under one instrument."""

    item: ArchivedNewsItem
    category: EventCategory
    sentiment: SentimentLabel
    #: Whether the link to this instrument was confident or contested. An
    #: ambiguous match is shown and marked rather than dropped: an ambiguity
    #: nobody sees is an ambiguity nobody reviews.
    match_state: MatchState

    @property
    def is_notable(self) -> bool:
        """Return whether the classifier named a specific kind of event."""
        return self.category not in _UNREMARKABLE

    @property
    def sort_key(self) -> tuple[int, float, str]:
        """Rank by the classifier's own precedence, then by recency."""
        published = self.item.revision.item.published_at
        return (
            event_precedence(self.category),
            -published.timestamp(),
            self.item.revision.revision,
        )


@dataclass(frozen=True, slots=True)
class DigestSection:
    """Everything stored about one instrument inside the window."""

    instrument_id: InstrumentId
    canonical_symbol: str
    company_name: str
    entries: tuple[DigestEntry, ...]
    #: How many entries exist beyond the ones shown. Reported rather than
    #: hidden, so a truncated section cannot be read as a complete one.
    withheld: int = 0

    def __post_init__(self) -> None:
        """Require a countable, non-negative remainder."""
        invariant(self.withheld >= 0, "a withheld count cannot be negative")

    @property
    def is_quiet(self) -> bool:
        """Return whether nothing at all was stored for this instrument."""
        return not self.entries

    @property
    def notable(self) -> tuple[DigestEntry, ...]:
        """Return the entries the classifier gave a specific category."""
        return tuple(entry for entry in self.entries if entry.is_notable)

    @property
    def categories(self) -> tuple[EventCategory, ...]:
        """Return the distinct categories present, most significant first."""
        seen = {entry.category for entry in self.entries}
        return tuple(sorted(seen, key=event_precedence))

    @property
    def sentiments(self) -> tuple[tuple[SentimentLabel, int], ...]:
        """Return how many entries carry each sentiment verdict.

        Counted, never averaged. A mean of POSITIVE and NEGATIVE is NEUTRAL,
        which is the one thing a split verdict does not mean.
        """
        tally: dict[SentimentLabel, int] = {}
        for entry in self.entries:
            tally[entry.sentiment] = tally.get(entry.sentiment, 0) + 1
        return tuple(sorted(tally.items(), key=lambda pair: (-pair[1], pair[0].value)))

    @property
    def sort_key(self) -> tuple[int, str]:
        """Rank instruments by the most significant thing stored about them."""
        best = min(
            (event_precedence(entry.category) for entry in self.entries),
            default=_QUIET_PRECEDENCE,
        )
        return (best, self.canonical_symbol)


#: Sorts a quiet instrument below every instrument that has anything at all,
#: including one carrying only commentary. Nothing stored is less of an answer
#: to "what changed?" than commentary is, and it belongs at the bottom.
_QUIET_PRECEDENCE = 1_000


@dataclass(frozen=True, slots=True)
class WatchlistDigest:
    """One section per watchlist instrument, as the archive stood at a cutoff."""

    known_at: datetime
    published_from: datetime
    published_to: datetime
    sections: tuple[DigestSection, ...]
    revision: str = DIGEST_REVISION

    @property
    def quiet(self) -> tuple[DigestSection, ...]:
        """Return the instruments nothing was stored about."""
        return tuple(section for section in self.sections if section.is_quiet)

    @property
    def items_reported(self) -> int:
        """Return how many entries are shown across every section."""
        return sum(len(section.entries) for section in self.sections)


def build_digest(  # noqa: PLR0913 - each argument names one axis of the question
    instruments: Iterable[LinkableInstrument],
    items: Iterable[ArchivedNewsItem],
    *,
    known_at: datetime,
    published_from: datetime,
    published_to: datetime,
    max_items: int = MAX_ITEMS_PER_INSTRUMENT,
) -> WatchlistDigest:
    """Group stored items under the instruments their analyses already link to.

    An item reaches an instrument only through a link the entity-linking ruleset
    recorded at ingestion. Nothing here re-reads a headline, so an item that
    matched nothing appears in no section -- which is the correct outcome, and
    is why the poll command reports its own unresolved count separately.

    Raises
    ------
    ValidationError
        If ``max_items`` is not a positive count.
    """
    if max_items < 1:
        raise ValidationError("a digest must show at least one item", max_items=max_items)

    grouped: dict[str, list[DigestEntry]] = {}
    for item in items:
        analysis = item.analysis
        if analysis is None:
            # A revision stored without an analysis carries no link and no
            # verdict. It is not evidence about any instrument yet.
            continue
        for match, state in analysis.linked:
            grouped.setdefault(match.canonical_symbol, []).append(
                DigestEntry(
                    item=item,
                    category=analysis.event.category,
                    sentiment=analysis.sentiment.label,
                    match_state=state,
                )
            )

    sections = []
    for instrument in instruments:
        found = sorted(grouped.get(instrument.canonical_symbol, ()), key=lambda e: e.sort_key)
        sections.append(
            DigestSection(
                instrument_id=instrument.instrument_id,
                canonical_symbol=instrument.canonical_symbol,
                company_name=instrument.company_name,
                entries=tuple(found[:max_items]),
                withheld=max(len(found) - max_items, 0),
            )
        )

    return WatchlistDigest(
        known_at=known_at,
        published_from=published_from,
        published_to=published_to,
        sections=tuple(sorted(sections, key=lambda section: section.sort_key)),
    )
