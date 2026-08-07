"""Render a watchlist digest as text, with attribution and without advice.

The hardest constraint here is what the output must *not* say. A digest that
ranks instruments and highlights findings is one careless sentence away from
reading as a recommendation, so nothing in this module contains a verb like buy,
sell, hold or target, and every section is explicit that it reports what was
recorded rather than what to do about it.

Ordering is significance-first and is stated as such, because a reader who
assumes alphabetical order and finds fraud at the top will misread the list.

Market context, where it is available, is printed next to the news rather than
merged into it. The two are different kinds of fact -- one is what a publisher
wrote, the other is what a close did -- and a layout that blurred them would
invite the reader to treat a percentage as an explanation of a headline. Every
absence, staleness and short history is stated in the line where the number
would otherwise have been.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.domain.entity_linking import MatchState
from dhruva.contexts.intelligence.interfaces.news_presentation import NSE_UNAVAILABLE_NOTICE

if TYPE_CHECKING:
    from collections.abc import Mapping

    from dhruva.contexts.intelligence.domain.digest import (
        DigestEntry,
        DigestSection,
        WatchlistDigest,
    )
    from dhruva.contexts.marketdata.api import MarketContext
    from dhruva.shared.identity import InstrumentId

__all__ = [
    "DIGEST_DISCLAIMER",
    "render_digest",
    "render_market_context",
    "render_section",
]

#: Printed once per digest. The ordering claim is the load-bearing part: a
#: reader who assumes alphabetical order will read the top of the list as a
#: recommendation rather than as the classifier's precedence.
DIGEST_DISCLAIMER = (
    "This is a record of what DHRUVA stored, not advice and not a "
    "recommendation. Sections are ordered by the event classifier's own "
    "precedence, not by any view on the instruments. Sentiment is a "
    "deterministic lexical baseline over headlines and cannot on its own "
    "support a trading decision. Market figures are arithmetic over stored "
    "daily closes, not a view on value."
)

_INDENT = "    "
_REVISION_PREFIX = 8
_RULE = "-" * 72


def render_entry(entry: DigestEntry) -> str:
    """Render one stored item under one instrument."""
    news = entry.item.revision.item
    marker = "*" if entry.is_notable else " "
    ambiguous = "" if entry.match_state is MatchState.MATCHED else "  [AMBIGUOUS LINK]"
    return "\n".join(
        (
            f"{_INDENT}{marker} {news.published_at.isoformat()}  "
            f"{entry.category}  {entry.sentiment}{ambiguous}",
            f"{_INDENT}  {news.text.title}",
            f"{_INDENT}  {news.identity.url}",
            f"{_INDENT}  source: {news.source.display_name} "
            f"({news.source.key} -- {news.source.homepage_url})",
            f"{_INDENT}  first seen {news.first_seen_at.isoformat()}  "
            f"revision {entry.item.revision.revision[:_REVISION_PREFIX]}",
        )
    )


def render_market_context(context: MarketContext | None) -> list[str]:
    """Render the price lines for one instrument, or say why there are none.

    Returns a list so a caller can place it inside a section; an empty list is
    never returned, because "we did not look" and "we looked and there was
    nothing" must not render identically.
    """
    if context is None:
        return [f"{_INDENT}market   : not requested"]
    if not context.has_prices:
        return [f"{_INDENT}market   : no data -- {context.limitation}"]

    stale = " [STALE]" if context.is_stale else ""
    age = "" if context.staleness_days is None else f", {context.staleness_days}d before cutoff"
    lines = [
        f"{_INDENT}market   : close {context.latest_close} on {context.latest_date}{stale}{age}",
    ]
    lines.append(f"{_INDENT}           {_change_line(context)}")
    lines.append(f"{_INDENT}           {_volume_line(context)}")
    if context.limitation is not None:
        lines.append(f"{_INDENT}           note: {context.limitation}")
    return lines


def _change_line(context: MarketContext) -> str:
    """Render the close-to-close changes, naming any that could not be computed."""
    if context.one_day_change_percent is None:
        one_day = "1d n/a"
    else:
        one_day = f"1d {_signed(context.one_day_change_percent)}%"
    if context.multi_day_change_percent is None or context.multi_day_sessions is None:
        multi = "multi-day n/a"
    else:
        multi = f"{context.multi_day_sessions}d {_signed(context.multi_day_change_percent)}%"
    bars = "1 bar" if context.bars_available == 1 else f"{context.bars_available} bars"
    return f"{one_day}   {multi}   [{context.availability}, {bars}]"


def _volume_line(context: MarketContext) -> str:
    """Render the volume and its comparison, or say the comparison is absent."""
    volume = "volume n/a" if context.latest_volume is None else f"volume {context.latest_volume:,}"
    if context.volume_ratio is None or context.volume_baseline_sessions is None:
        return f"{volume}   vs recent n/a"
    return (
        f"{volume}   {context.volume_ratio}x the mean of the "
        f"prior {context.volume_baseline_sessions} sessions"
    )


def _signed(value: object) -> str:
    """Render a percentage with an explicit sign, so a fall cannot read as a rise."""
    return f"+{value}" if not str(value).startswith("-") else str(value)


def render_section(
    section: DigestSection,
    context: MarketContext | None = None,
) -> str:
    """Render one instrument, including when there is nothing to report.

    Market context is shown even when the news side is silent: "nothing was
    written about it and it fell 4%" is a materially different morning from
    "nothing was written about it and it did not move".
    """
    heading = f"{section.canonical_symbol}  --  {section.company_name}"
    market = render_market_context(context)

    if section.is_quiet:
        return "\n".join([heading, *market, f"{_INDENT}news     : nothing archived in this window"])

    lines = [heading, *market]
    categories = ", ".join(str(category) for category in section.categories)
    tally = ", ".join(f"{label} {count}" for label, count in section.sentiments)
    lines.append(f"{_INDENT}events   : {categories}")
    lines.append(f"{_INDENT}sentiment: {tally}")
    lines.append("")
    lines.extend(render_entry(entry) + "\n" for entry in section.entries)
    if section.withheld:
        lines.append(f"{_INDENT}... and {section.withheld} more not shown")
    return "\n".join(lines).rstrip()


def render_digest(
    digest: WatchlistDigest,
    contexts: Mapping[InstrumentId, MarketContext] | None = None,
) -> str:
    """Render the whole digest, quiet instruments included."""
    reported = digest.items_reported
    quiet = len(digest.quiet)
    lines = [
        f"Watchlist digest as known at {digest.known_at.isoformat()}",
        f"published between {digest.published_from.isoformat()} "
        f"and {digest.published_to.isoformat()}",
        f"{len(digest.sections)} instruments, {reported} items shown, "
        f"{quiet} with nothing archived  [{digest.revision}]",
        "",
        DIGEST_DISCLAIMER,
        "",
        _RULE,
        "",
    ]
    if not digest.sections:
        lines.append("No instruments are on the watchlist at this cutoff.")
    else:
        rendered = (
            render_section(
                section, None if contexts is None else contexts.get(section.instrument_id)
            )
            for section in digest.sections
        )
        lines.append(f"\n\n{_RULE}\n\n".join(rendered))
    lines.extend(("", _RULE, "", NSE_UNAVAILABLE_NOTICE))
    return "\n".join(lines)
