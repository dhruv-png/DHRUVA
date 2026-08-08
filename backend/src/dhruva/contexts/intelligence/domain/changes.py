"""Deterministic differences between two already-resolved research states.

Answers one question: "what changed for the watchlist between two research
cutoffs?" Every fact reported here was already computed once -- by
:func:`~dhruva.contexts.intelligence.interfaces.attention_presentation.rank_watchlist`
at each cutoff independently -- and this module only compares those two
already-correct answers. It recomputes nothing from a stored bar or a stored
headline: doing that would be a second opinion about a fact this codebase
already has one opinion about, and the two would eventually disagree.

**Two cutoffs, two independent reads.** ``before`` and ``after`` are each
assumed to already be point-in-time correct for their own cutoff -- read by
two separate calls to ``rank_watchlist``/``BuildWatchlistDigest``, each with
its own ``known_at``. This module performs no filtering by knowledge time of
its own, for the identical reason
:mod:`dhruva.contexts.intelligence.domain.attention` does not: a change
report computed from anything but two already-PIT-resolved states would leak
the later cutoff's knowledge into the earlier one.

**No raw market data.** Every market-facing fact a change can report --
score, band, whether market context was available at all -- is already a
field on :class:`~dhruva.contexts.intelligence.domain.attention.ResearchAttention`,
so this module needs no ``MarketContext`` and stays inside the intelligence
domain's own boundary (ADR-001, enforced by ``test_domain_purity.py``): it
reaches no other context, exactly as
:mod:`dhruva.contexts.intelligence.domain.attention` does not either.

**Absence of a change is not reported.** An instrument whose verdict is
identical at both cutoffs contributes nothing -- not a line, not an empty
category list. A change report that named every unchanged instrument would
bury the ones that did.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import datetime

    from dhruva.contexts.intelligence.domain.attention import ResearchAttention
    from dhruva.contexts.intelligence.domain.digest import (
        DigestEntry,
        DigestSection,
        WatchlistDigest,
    )
    from dhruva.shared.identity import InstrumentId

__all__ = [
    "CHANGES_REVISION",
    "ChangeCategory",
    "ResearchChange",
    "compare_instrument",
    "compare_watchlist",
    "new_news_since",
]

#: Ruleset identity recorded on every result, so a future change of
#: categories or ordering is visible in an operator's output rather than only
#: in this file's history.
CHANGES_REVISION = "watchlist-changes-v1"


class ChangeCategory(StrEnum):
    """One deterministic, mechanically-derived kind of difference.

    Every member names a fact a reader can check against the two verdicts
    directly -- never an interpretation of whether the change was good or
    bad. ``ENTERED``/``EXITED`` describe the attention set (``score > 0``),
    not watchlist membership, which this module does not compare.
    """

    #: Score was 0 at the earlier cutoff and is positive at the later one.
    ENTERED = "ENTERED"
    #: Score was positive at the earlier cutoff and is 0 at the later one.
    EXITED = "EXITED"
    SCORE_INCREASED = "SCORE_INCREASED"
    SCORE_DECREASED = "SCORE_DECREASED"
    BAND_CHANGED = "BAND_CHANGED"
    #: At least one archived item was first seen strictly after the earlier
    #: cutoff and at or before the later one.
    NEWS_ADDED = "NEWS_ADDED"
    MARKET_CONTEXT_ADDED = "MARKET_CONTEXT_ADDED"
    MARKET_CONTEXT_REMOVED = "MARKET_CONTEXT_REMOVED"


#: Fixed report order -- set membership first, then magnitude, then
#: supporting detail -- so two runs over the same two states print categories
#: in the same order rather than in whatever order they were detected.
_CATEGORY_ORDER = (
    ChangeCategory.ENTERED,
    ChangeCategory.EXITED,
    ChangeCategory.SCORE_INCREASED,
    ChangeCategory.SCORE_DECREASED,
    ChangeCategory.BAND_CHANGED,
    ChangeCategory.NEWS_ADDED,
    ChangeCategory.MARKET_CONTEXT_ADDED,
    ChangeCategory.MARKET_CONTEXT_REMOVED,
)


@dataclass(frozen=True, slots=True)
class ResearchChange:
    """What differs about one instrument between two research cutoffs.

    Every field is mechanically derived from ``before`` and ``after`` --
    nothing here is a second opinion about what the scores or the archive
    already said, and nothing interprets a change as good, bad, or worth
    acting on.
    """

    instrument_id: InstrumentId
    canonical_symbol: str
    company_name: str
    before: ResearchAttention
    after: ResearchAttention
    #: At least one category is always present; see ``__post_init__``.
    categories: tuple[ChangeCategory, ...]
    #: Reasons present in ``after.reasons`` that were absent from
    #: ``before.reasons``, in ``after``'s own order. Compared as exact
    #: strings, which is precise because every reason is itself a
    #: deterministic, fully-parameterised sentence.
    new_reasons: tuple[str, ...]
    #: Entries first seen strictly after ``before.as_of`` -- news the
    #: earlier cutoff could not have known about yet.
    new_news: tuple[DigestEntry, ...]
    revision: str = CHANGES_REVISION

    def __post_init__(self) -> None:
        """Require at least one category -- an unchanged instrument is not reported."""
        invariant(len(self.categories) > 0, "a research change must name at least one category")


def new_news_since(section: DigestSection, *, since: datetime) -> tuple[DigestEntry, ...]:
    """Return the entries of ``section`` first seen strictly after ``since``.

    ``section`` is assumed already read point-in-time at some later cutoff,
    so every entry it holds already satisfies ``first_seen_at <= that
    cutoff`` -- this checks only the lower bound, which is exactly "new since
    the earlier cutoff."
    """
    return tuple(
        entry for entry in section.entries if entry.item.revision.item.first_seen_at > since
    )


def compare_instrument(
    before: ResearchAttention,
    after: ResearchAttention,
    *,
    new_news: tuple[DigestEntry, ...] = (),
) -> ResearchChange | None:
    """Return what changed for one instrument, or ``None`` if nothing did.

    ``new_news`` is supplied by the caller, computed from the *after*-cutoff
    digest section via :func:`new_news_since` -- this function performs no
    point-in-time filtering of its own, for the same reason
    :func:`~dhruva.contexts.intelligence.interfaces.attention_presentation.rank_watchlist`
    does not either.
    """
    found: set[ChangeCategory] = set()
    was_attention = before.score > 0
    is_attention = after.score > 0
    if not was_attention and is_attention:
        found.add(ChangeCategory.ENTERED)
    if was_attention and not is_attention:
        found.add(ChangeCategory.EXITED)
    if after.score > before.score:
        found.add(ChangeCategory.SCORE_INCREASED)
    if after.score < before.score:
        found.add(ChangeCategory.SCORE_DECREASED)
    if after.band is not before.band:
        found.add(ChangeCategory.BAND_CHANGED)
    if new_news:
        found.add(ChangeCategory.NEWS_ADDED)
    if not before.market_context_available and after.market_context_available:
        found.add(ChangeCategory.MARKET_CONTEXT_ADDED)
    if before.market_context_available and not after.market_context_available:
        found.add(ChangeCategory.MARKET_CONTEXT_REMOVED)

    ordered = tuple(category for category in _CATEGORY_ORDER if category in found)
    if not ordered:
        return None

    new_reasons = tuple(reason for reason in after.reasons if reason not in before.reasons)
    return ResearchChange(
        instrument_id=after.instrument_id,
        canonical_symbol=after.canonical_symbol,
        company_name=after.company_name,
        before=before,
        after=after,
        categories=ordered,
        new_reasons=new_reasons,
        new_news=new_news,
    )


def compare_watchlist(
    before: tuple[ResearchAttention, ...],
    after: tuple[ResearchAttention, ...],
    *,
    after_digest: WatchlistDigest,
) -> tuple[ResearchChange, ...]:
    """Return one change per instrument that differs, ordered deterministically.

    ``before``/``after`` are two independently-resolved
    :func:`~dhruva.contexts.intelligence.interfaces.attention_presentation.rank_watchlist`
    outputs, one per cutoff -- this performs no ranking, filtering or
    point-in-time logic of its own; both inputs were already correct as of
    their own cutoff before they reached here. An instrument present only in
    ``before`` (the watchlist changed between the two cutoffs) is outside
    this module's scope and is skipped rather than guessed at.

    Ordered by the size of the score movement, largest first, then
    alphabetically -- the same tie-break
    :attr:`~dhruva.contexts.intelligence.domain.attention.ResearchAttention.sort_key`
    uses, so the biggest movers are the ones a reader sees first.
    """
    before_by_instrument = {entry.instrument_id: entry for entry in before}
    after_sections = {section.instrument_id: section for section in after_digest.sections}

    changes = []
    for after_entry in after:
        before_entry = before_by_instrument.get(after_entry.instrument_id)
        if before_entry is None:
            continue
        section = after_sections.get(after_entry.instrument_id)
        new_news = () if section is None else new_news_since(section, since=before_entry.as_of)
        change = compare_instrument(before_entry, after_entry, new_news=new_news)
        if change is not None:
            changes.append(change)

    return tuple(
        sorted(
            changes,
            key=lambda change: (
                -abs(change.after.score - change.before.score),
                change.canonical_symbol,
            ),
        )
    )
