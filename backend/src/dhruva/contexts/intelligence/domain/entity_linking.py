"""Link a headline to owner-approved instruments, or admit that it did not.

Three answers only: matched, ambiguous, unresolved. The expensive mistake in
news mapping is not a missed link, it is a confident wrong one -- a headline
about a company DHRUVA does not follow, attached to one it does, becomes
evidence in a scan that nobody can trace back. So every rule here is bounded and
every result carries the reason and the ruleset revision that produced it.

Aliases never replace canonical symbols. They are additional ways in, and every
match resolves to the stable instrument identity and its canonical symbol.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import date

    from dhruva.shared.identity import InstrumentId

__all__ = [
    "ENTITY_LINKING_REVISION",
    "FORMER_NAME_GRACE",
    "EntityLinkResult",
    "EntityMatch",
    "HistoricalName",
    "LinkableInstrument",
    "MatchKind",
    "MatchState",
    "link_entities",
]

#: Ruleset identity recorded on every link result.
ENTITY_LINKING_REVISION = "news-entity-linking-v1"

#: How long a former name stays usable after the company stopped using it.
#:
#: The press does not rename a company on the day its shareholders do. A year is
#: long enough to catch "Zomato, now Eternal, said..." and short enough that a
#: name reused by somebody else years later cannot quietly inherit the mapping.
FORMER_NAME_GRACE = timedelta(days=365)

#: Corporate suffixes a headline drops. Stripped only when at least this many
#: tokens survive, so "Eternal Limited" never becomes the adjective "eternal"
#: while "ICICI Bank Limited" still matches the way every headline writes it.
_CORPORATE_SUFFIXES = ("limited", "ltd", "plc", "inc", "corporation", "corp")
_MIN_SHORTENED_NAME_TOKENS = 2

#: Fraction of letters in caps above which a headline is shouting rather than
#: naming a ticker, making letter case useless as evidence.
_SHOUTING_RATIO = Decimal("0.7")
_MIN_SHOUTING_LENGTH = 12

#: Symbols keep ``&`` and ``-`` so ``M&M`` and ``NAM-INDIA`` survive tokenising
#: as single tokens rather than becoming ``m``, ``m`` and ``nam``, ``india``.
_SYMBOL_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9&.-]*")
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

_RELEVANCE = {
    "CANONICAL_SYMBOL": Decimal("1.00"),
    "COMPANY_NAME": Decimal("0.90"),
    "ALIAS": Decimal("0.80"),
    "FORMER_NAME": Decimal("0.60"),
}


class MatchState(StrEnum):
    """Whether the headline could be tied to instruments with confidence."""

    MATCHED = "MATCHED"
    AMBIGUOUS = "AMBIGUOUS"
    UNRESOLVED = "UNRESOLVED"


class MatchKind(StrEnum):
    """Which kind of name the headline actually used."""

    CANONICAL_SYMBOL = "CANONICAL_SYMBOL"
    COMPANY_NAME = "COMPANY_NAME"
    ALIAS = "ALIAS"
    FORMER_NAME = "FORMER_NAME"


@dataclass(frozen=True, slots=True)
class HistoricalName:
    """A name the company no longer uses, and the day it stopped."""

    text: str
    valid_to: date

    def __post_init__(self) -> None:
        """Require a usable historical name."""
        invariant(bool(self.text.strip()), "a historical name must not be empty")

    def eligible_on(self, day: date) -> bool:
        """Return whether this name may still be read as naming the company."""
        return day <= self.valid_to + FORMER_NAME_GRACE


@dataclass(frozen=True, slots=True)
class LinkableInstrument:
    """The names one approved instrument may be recognised by, and from when.

    Provider-neutral by construction. The reference context owns the effective
    identity; this is the projection of it that text matching needs, and nothing
    here knows how that identity is stored.
    """

    instrument_id: InstrumentId
    canonical_symbol: str
    company_name: str
    aliases: tuple[str, ...] = ()
    former_names: tuple[HistoricalName, ...] = ()
    valid_from: date | None = None

    def __post_init__(self) -> None:
        """Require a canonical symbol and a company name to match against."""
        invariant(bool(self.canonical_symbol.strip()), "canonical symbol must not be empty")
        invariant(
            self.canonical_symbol == self.canonical_symbol.upper(),
            "canonical symbol must be uppercase",
        )
        invariant(bool(self.company_name.strip()), "company name must not be empty")

    def existed_on(self, day: date) -> bool:
        """Return whether the instrument's identity applies on ``day``."""
        return self.valid_from is None or day >= self.valid_from


@dataclass(frozen=True, slots=True)
class EntityMatch:
    """One instrument the headline named, how it named it, and how strongly."""

    instrument_id: InstrumentId
    canonical_symbol: str
    matched_text: str
    kind: MatchKind
    relevance: Decimal
    reason: str

    def __post_init__(self) -> None:
        """Keep relevance a bounded score and the reason present."""
        invariant(Decimal(0) < self.relevance <= Decimal(1), "relevance must fall in (0, 1]")
        invariant(bool(self.reason), "a match must give a reason")


@dataclass(frozen=True, slots=True)
class EntityLinkResult:
    """The mapping verdict for one headline, with everything behind it."""

    state: MatchState
    matches: tuple[EntityMatch, ...]
    ambiguous: tuple[EntityMatch, ...]
    reason: str
    revision: str = ENTITY_LINKING_REVISION

    def __post_init__(self) -> None:
        """Keep the state and the evidence consistent with each other."""
        invariant(bool(self.reason), "a link result must give a reason")
        if self.state is MatchState.MATCHED:
            invariant(bool(self.matches), "a matched result must carry matches")
        elif self.state is MatchState.AMBIGUOUS:
            invariant(not self.matches, "an ambiguous result presents no confident match")
            invariant(bool(self.ambiguous), "an ambiguous result must carry its candidates")
        else:
            invariant(
                not self.matches and not self.ambiguous,
                "an unresolved result carries no candidates at all",
            )


def _normalise(value: str) -> str:
    """Fold text to the spacing and case that name phrases are compared in."""
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", value.casefold())).strip()


def _is_shouting(text: str) -> bool:
    """Return whether letter case in this text carries no information."""
    letters = [item for item in text if item.isalpha()]
    if len(letters) < _MIN_SHOUTING_LENGTH:
        return False
    upper = sum(1 for item in letters if item.isupper())
    return Decimal(upper) / Decimal(len(letters)) > _SHOUTING_RATIO


def _phrase_present(needle: str, haystack: str) -> bool:
    """Return whether a normalised phrase appears on whole-word boundaries."""
    folded = _normalise(needle)
    return bool(folded) and f" {folded} " in f" {haystack} "


def _name_forms(name: str) -> tuple[str, ...]:
    """Return the full company name and, where safe, its everyday short form."""
    tokens = _normalise(name).split()
    if len(tokens) > _MIN_SHORTENED_NAME_TOKENS and tokens[-1] in _CORPORATE_SUFFIXES:
        return (name, " ".join(tokens[:-1]))
    return (name,)


def _symbol_present(symbol: str, text: str, *, shouting: bool) -> bool:
    """Return whether a bare symbol appears as its own token, in ticker spelling.

    Exact upper case, always. ``ETERNAL`` is a ticker and an ordinary adjective;
    ``HAL``, ``PNB`` and ``M&M`` are tickers and initials. Requiring the ticker
    spelling is what stops "eternal optimism grips traders" from becoming a
    position, and a lower-case mention is nearly always the company name, which
    the name and alias rules read instead.

    When the whole headline is shouting, letter case proves nothing, so bare
    symbols are not read at all and the company name has to do the work.
    """
    if shouting:
        return False
    return symbol in _SYMBOL_TOKEN.findall(text)


def _candidates(
    text: str,
    instrument: LinkableInstrument,
    *,
    published_on: date,
    shouting: bool,
) -> EntityMatch | None:
    """Return the strongest way this headline named one instrument, if any."""
    folded = _normalise(text)
    if not instrument.existed_on(published_on):
        return None

    if _symbol_present(instrument.canonical_symbol, text, shouting=shouting):
        return _match(
            instrument,
            instrument.canonical_symbol,
            MatchKind.CANONICAL_SYMBOL,
            "the canonical symbol appears as its own token",
        )
    for form in _name_forms(instrument.company_name):
        if _phrase_present(form, folded):
            return _match(
                instrument,
                form,
                MatchKind.COMPANY_NAME,
                f"the company name '{form}' appears in full",
            )
    for alias in instrument.aliases:
        if _phrase_present(alias, folded):
            return _match(
                instrument,
                alias,
                MatchKind.ALIAS,
                f"the approved alias '{alias}' appears in full",
            )
    for former in instrument.former_names:
        if not _phrase_present(former.text, folded):
            continue
        if not former.eligible_on(published_on):
            continue
        return _match(
            instrument,
            former.text,
            MatchKind.FORMER_NAME,
            f"the former name '{former.text}' appears within its usable period",
        )
    return None


def _match(
    instrument: LinkableInstrument,
    matched_text: str,
    kind: MatchKind,
    reason: str,
) -> EntityMatch:
    """Build one match against an instrument's stable identity."""
    return EntityMatch(
        instrument_id=instrument.instrument_id,
        canonical_symbol=instrument.canonical_symbol,
        matched_text=matched_text,
        kind=kind,
        relevance=_RELEVANCE[kind.value],
        reason=reason,
    )


def link_entities(
    text: str,
    *,
    universe: Iterable[LinkableInstrument],
    published_on: date,
) -> EntityLinkResult:
    """Map a headline onto approved instruments at its publication date.

    Two instruments recognised through the *same* words is ambiguity, not two
    findings, and those candidates are reported separately from the confident
    matches so nothing downstream can mistake one for the other.
    """
    found = [
        item
        for item in (
            _candidates(text, instrument, published_on=published_on, shouting=_is_shouting(text))
            for instrument in universe
        )
        if item is not None
    ]
    if not found:
        return EntityLinkResult(
            state=MatchState.UNRESOLVED,
            matches=(),
            ambiguous=(),
            reason="no approved symbol, company name or usable alias appeared",
        )

    by_text: dict[str, list[EntityMatch]] = {}
    for item in found:
        by_text.setdefault(_normalise(item.matched_text), []).append(item)

    confident = tuple(group[0] for group in by_text.values() if len(group) == 1)
    contested = tuple(item for group in by_text.values() if len(group) > 1 for item in group)

    if confident:
        note = (
            " some other wording was ambiguous and is reported separately"
            if contested
            else " every match resolved to one instrument"
        )
        return EntityLinkResult(
            state=MatchState.MATCHED,
            matches=tuple(sorted(confident, key=lambda item: item.canonical_symbol)),
            ambiguous=tuple(sorted(contested, key=lambda item: item.canonical_symbol)),
            reason=f"matched {len(confident)} instrument(s);{note}",
        )
    return EntityLinkResult(
        state=MatchState.AMBIGUOUS,
        matches=(),
        ambiguous=tuple(sorted(contested, key=lambda item: item.canonical_symbol)),
        reason="the same wording names more than one approved instrument",
    )
