"""Compose and render research attention, with an explicit non-recommendation notice.

Three jobs, for the same reason ``digest_presentation`` and ``digest_export``
combine composition and output rather than splitting them: :func:`rank_watchlist`
is where a ``WatchlistDigest`` and a ``MarketContext`` mapping -- already
resolved point-in-time by their own callers -- are read together, and the
intelligence *domain* may not do that (ADR-001: it reaches no other context,
enforced by ``test_domain_purity.py``). Everything genuinely pure --
the points, the bands, the ``ResearchAttention`` type, and which entries a
brief keeps (:func:`~dhruva.contexts.intelligence.domain.attention.top_attention`)
-- stays in :mod:`dhruva.contexts.intelligence.domain.attention`; this module
only reads a ``MarketContext``'s fields and calls into that pure arithmetic.
:func:`render_brief` is the third job: a compact, top-N alternative to
:func:`render_attention` plus the full digest, showing the same ranking with
just enough market and news detail beside each entry to explain it, and
nothing that would need a second read to produce.

The same constraint ``digest_presentation`` is built around applies to
:func:`render_attention` with more force, not less: a list that puts one
symbol above another is the single easiest thing in this whole codebase to
misread as "buy this one first". Nothing in this module contains a verb like
buy, sell, hold, recommend or outperform, and the disclaimer states plainly
what the ordering *is* -- more observable change or archived coverage right
now -- so a reader does not have to guess what it is not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.domain.attention import (
    NEWS_ITEM_POINT_CAP,
    NOTABLE_EVENT_BONUS,
    ResearchAttention,
    band_for_score,
    move_points,
    volume_points,
)
from dhruva.contexts.intelligence.domain.events import EventCategory, event_precedence

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from dhruva.contexts.intelligence.domain.digest import DigestSection, WatchlistDigest
    from dhruva.contexts.marketdata.api import MarketContext
    from dhruva.shared.identity import InstrumentId

__all__ = ["ATTENTION_DISCLAIMER", "rank_watchlist", "render_attention", "render_brief"]

#: Printed once per rendering. States positively what the ranking measures and
#: negatively what it is never allowed to be read as -- both matter, because a
#: disclaimer that only says what something is not still lets a reader guess
#: at what it is.
ATTENTION_DISCLAIMER = (
    "Attention ranks where more observable change or archived coverage may "
    "warrant review. It is not advice and not a recommendation: not an "
    "expected return, not a signal to buy, sell or hold, and not a view on "
    "which instrument is the better investment. The score counts the "
    "magnitude of stored price movement, volume against its own recent "
    "baseline, and archived news coverage -- arithmetic and counting over "
    "what is already stored, never a prediction of what happens next."
)

_INDENT = "    "
_RULE = "-" * 72


def render_attention(ranked: Sequence[ResearchAttention]) -> str:
    """Render one compact, ordered line per instrument, plus its reasons.

    Ties are already broken deterministically by :func:`rank_watchlist`
    (score, then canonical symbol), so this function does no sorting of its
    own -- the order it is handed is the order it prints.
    """
    lines = [
        "Research attention -- most noteworthy observable context first",
    ]
    if ranked:
        lines.append(f"as of {ranked[0].as_of.isoformat()}  [{ranked[0].revision}]")
    lines.extend(("", ATTENTION_DISCLAIMER, "", _RULE, ""))

    if not ranked:
        lines.append("No instruments are on the watchlist at this cutoff.")
        return "\n".join(lines)

    for entry in ranked:
        market = "" if entry.market_context_available else "  [market context unavailable]"
        reasons = "; ".join(entry.reasons) if entry.reasons else "nothing observed was noteworthy"
        lines.append(
            f"{entry.canonical_symbol:<12} {entry.band.value:<10} score {entry.score}{market}"
        )
        lines.append(f"{_INDENT}{reasons}")
    return "\n".join(lines)


def rank_watchlist(
    digest: WatchlistDigest,
    contexts: Mapping[InstrumentId, MarketContext] | None,
) -> tuple[ResearchAttention, ...]:
    """Return one verdict per digest section, ordered by attention.

    Reads only ``digest`` and ``contexts`` -- both already resolved
    point-in-time by their own callers, exactly as
    :func:`~dhruva.contexts.intelligence.interfaces.digest_presentation.render_digest`
    reads them. This function performs no filtering by knowledge time itself,
    for the same reason ``render_digest`` does not: doing it twice in two
    places is how the two eventually disagree.
    """
    ranked = tuple(
        _attention_for(
            section,
            None if contexts is None else contexts.get(section.instrument_id),
            as_of=digest.known_at,
        )
        for section in digest.sections
    )
    return tuple(sorted(ranked, key=lambda item: item.sort_key))


def _attention_for(
    section: DigestSection,
    context: MarketContext | None,
    *,
    as_of: datetime,
) -> ResearchAttention:
    """Score one instrument from its digest section and its market context."""
    reasons: list[str] = []
    score = 0

    market_context_available = context is not None and context.has_prices
    if market_context_available:
        assert context is not None  # noqa: S101 - narrowed by the check above
        if context.one_day_change_percent is not None:
            points, reason = move_points(context.one_day_change_percent, label="1-day")
            score += points
            if reason is not None:
                reasons.append(reason)
        if context.multi_day_change_percent is not None and context.multi_day_sessions is not None:
            points, reason = move_points(
                context.multi_day_change_percent,
                label=f"{context.multi_day_sessions}-session",
            )
            score += points
            if reason is not None:
                reasons.append(reason)
        if context.volume_ratio is not None and context.volume_baseline_sessions is not None:
            points, reason = volume_points(context.volume_ratio, context.volume_baseline_sessions)
            score += points
            if reason is not None:
                reasons.append(reason)

    item_count = len(section.entries)
    if item_count > 0:
        news_points = min(item_count, NEWS_ITEM_POINT_CAP)
        score += news_points
        noun = "item" if item_count == 1 else "items"
        reasons.append(f"{item_count} recent archived news {noun}")

        # The most significant category present, exactly as the digest itself
        # would rank it -- never a second opinion about severity.
        leading = min(section.categories, key=event_precedence)
        if leading not in {EventCategory.GENERAL_COMMENTARY, EventCategory.UNKNOWN}:
            score += NOTABLE_EVENT_BONUS
        reasons.append(f"event class {leading}")

    return ResearchAttention(
        instrument_id=section.instrument_id,
        canonical_symbol=section.canonical_symbol,
        company_name=section.company_name,
        as_of=as_of,
        score=score,
        band=band_for_score(score),
        reasons=tuple(reasons),
        market_context_available=market_context_available,
    )


def render_brief(
    top: Sequence[ResearchAttention],
    *,
    digest: WatchlistDigest,
    contexts: Mapping[InstrumentId, MarketContext] | None,
    total_ranked: int,
) -> str:
    """Render the leading noteworthy instruments: why, then market, then news.

    ``top`` is expected to already be the result of :func:`~dhruva.contexts.
    intelligence.domain.attention.top_attention` over :func:`rank_watchlist`'s
    output -- this function performs no ranking or filtering of its own. It
    reads ``digest`` and ``contexts`` only to look up the section and market
    context each entry in ``top`` belongs to; both were already resolved
    point-in-time by the caller that built them, exactly as
    :func:`rank_watchlist` reads them. No second, independent read happens
    here, so a brief cannot show a later bar or a later article than the
    ranking beside it did.

    ``total_ranked`` is the size of the full ranking ``top`` was drawn from,
    carried through only so the header can say how many instruments were not
    shown -- never recomputed, because a caller re-deriving it from ``digest``
    would eventually disagree with the ranking that produced ``top``.
    """
    sections = {section.instrument_id: section for section in digest.sections}
    lines = [f"Research brief as known at {digest.known_at.isoformat()}"]
    if top:
        lines.append(f"[{top[0].revision}]")
    lines.extend(("", ATTENTION_DISCLAIMER, "", _RULE, ""))

    if not top:
        if total_ranked == 0:
            lines.append("No instruments are on the watchlist at this cutoff.")
        else:
            lines.append("No instrument has observable research attention at this cutoff.")
        return "\n".join(lines)

    shown = "instrument" if len(top) == 1 else "instruments"
    lines.append(f"{len(top)} of {total_ranked} watchlist {shown} shown, most noteworthy first")
    lines.append("")
    for index, entry in enumerate(top, start=1):
        lines.extend(
            _brief_entry(
                index,
                entry,
                section=sections.get(entry.instrument_id),
                context=None if contexts is None else contexts.get(entry.instrument_id),
            )
        )
        lines.append("")
    return "\n".join(lines).rstrip()


def _brief_entry(
    index: int,
    entry: ResearchAttention,
    *,
    section: DigestSection | None,
    context: MarketContext | None,
) -> list[str]:
    """Render one ranked instrument: its verdict, then why, market and news."""
    lines = [f"{index}. {entry.canonical_symbol}  --  {entry.band.value} ({entry.score})"]
    lines.append(f"{_INDENT}why:")
    lines.extend(f"{_INDENT * 2}- {reason}" for reason in entry.reasons)
    lines.append(f"{_INDENT}market:")
    lines.extend(_brief_market_lines(context))
    lines.append(f"{_INDENT}news:")
    lines.extend(_brief_news_lines(section))
    return lines


def _brief_market_lines(context: MarketContext | None) -> list[str]:
    """Render the market facts a brief shows, or say why there are none.

    The same two absences ``render_market_context`` distinguishes: not asked
    for, versus asked for and nothing knowable. Collapsing them would let a
    brief taken with ``--no-market`` read as evidence that no prices existed.
    """
    if context is None:
        return [f"{_INDENT * 2}- not requested"]
    if not context.has_prices:
        return [f"{_INDENT * 2}- no data -- {context.limitation}"]
    return [f"{_INDENT * 2}- close {context.latest_close} on {context.latest_date}"]


def _brief_news_lines(section: DigestSection | None) -> list[str]:
    """Render the minimal, citable facts already stored about each item.

    Deliberately smaller than :func:`~dhruva.contexts.intelligence.interfaces.
    digest_presentation.render_entry`: a brief exists to be short, so it shows
    the category, the sentiment label, when it was published, the headline and
    where it came from -- and stops there. No prose summary is generated and
    no article body is read; both would need a fetch or a model this command
    does not have.
    """
    if section is None or not section.entries:
        return [f"{_INDENT * 2}- none archived"]
    lines: list[str] = []
    for item in section.entries:
        news = item.item.revision.item
        lines.append(
            f"{_INDENT * 2}- {item.category}  sentiment {item.sentiment}  "
            f"published {news.published_at.isoformat()}"
        )
        lines.append(f"{_INDENT * 3}{news.text.title}")
        lines.append(f"{_INDENT * 3}{news.identity.url}")
        lines.append(f"{_INDENT * 3}source: {news.source.display_name} ({news.source.key})")
    return lines
