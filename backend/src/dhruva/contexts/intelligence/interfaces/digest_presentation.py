"""Render a watchlist digest as text, with attribution and without advice.

The hardest constraint here is what the output must *not* say. A digest that
ranks instruments and highlights findings is one careless sentence away from
reading as a recommendation, so nothing in this module contains a verb like buy,
sell, hold or target, and every section is explicit that it reports what was
recorded rather than what to do about it.

Ordering is significance-first and is stated as such, because a reader who
assumes alphabetical order and finds fraud at the top will misread the list.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.domain.entity_linking import MatchState
from dhruva.contexts.intelligence.interfaces.news_presentation import NSE_UNAVAILABLE_NOTICE

if TYPE_CHECKING:
    from dhruva.contexts.intelligence.domain.digest import (
        DigestEntry,
        DigestSection,
        WatchlistDigest,
    )

__all__ = ["DIGEST_DISCLAIMER", "render_digest", "render_section"]

#: Printed once per digest. The ordering claim is the load-bearing part: a
#: reader who assumes alphabetical order will read the top of the list as a
#: recommendation rather than as the classifier's precedence.
DIGEST_DISCLAIMER = (
    "This is a record of what DHRUVA stored, not advice and not a "
    "recommendation. Sections are ordered by the event classifier's own "
    "precedence, not by any view on the instruments. Sentiment is a "
    "deterministic lexical baseline over headlines and cannot on its own "
    "support a trading decision."
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


def render_section(section: DigestSection) -> str:
    """Render one instrument, including when there is nothing to report."""
    heading = f"{section.canonical_symbol}  --  {section.company_name}"
    if section.is_quiet:
        return f"{heading}\n{_INDENT}nothing archived in this window"

    lines = [heading]
    categories = ", ".join(str(category) for category in section.categories)
    tally = ", ".join(f"{label} {count}" for label, count in section.sentiments)
    lines.append(f"{_INDENT}events   : {categories}")
    lines.append(f"{_INDENT}sentiment: {tally}")
    lines.append("")
    lines.extend(render_entry(entry) + "\n" for entry in section.entries)
    if section.withheld:
        lines.append(f"{_INDENT}... and {section.withheld} more not shown")
    return "\n".join(lines).rstrip()


def render_digest(digest: WatchlistDigest) -> str:
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
        lines.append(f"\n\n{_RULE}\n\n".join(render_section(s) for s in digest.sections))
    lines.extend(("", _RULE, "", NSE_UNAVAILABLE_NOTICE))
    return "\n".join(lines)
