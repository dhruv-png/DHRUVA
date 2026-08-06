"""Deterministic search phrases for discovering news about approved instruments.

Entity linking answers "does this headline name something we follow?". This
module answers the question before it: "what should we ask a news source for?".
Both are pure name reasoning over the owner-approved universe, and neither knows
what a provider's query syntax looks like -- a batch here is an ordered tuple of
phrases, and turning one into a provider expression is an adapter's job.

Three rules carry the weight.

**A bare exchange symbol is never a phrase.** ``HAL``, ``PNB``, ``SBI`` and
``M&M`` are precise inside NSE and ambiguous everywhere else; searching news for
``HAL`` returns the computer from *2001*. Symbols identify instruments, company
names find articles, and conflating the two is how a scan acquires evidence
about the wrong company.

**Nothing is dropped quietly.** A phrase that fails the safety rules is returned
with the reason it failed, and an instrument left with no phrase at all is
returned as unqueryable. An operator can see that ``SBIN`` is being searched by
company name and that its ``SBI`` alias was refused as too ambiguous; silence
would leave them believing coverage they do not have.

**The same watchlist always produces the same plan.** Ordering is by canonical
symbol and then by a fixed candidate order, so two runs an hour apart issue
identical requests and a diff between two plans means the watchlist changed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.domain.entity_linking import MatchKind
from dhruva.shared.errors import ValidationError
from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import date

    from dhruva.contexts.intelligence.domain.entity_linking import LinkableInstrument
    from dhruva.shared.identity import InstrumentId

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "MAX_BATCH_SIZE",
    "MAX_PHRASES_PER_INSTRUMENT",
    "MAX_PHRASE_CHARACTERS",
    "MIN_BATCH_SIZE",
    "MIN_SINGLE_TOKEN_CHARACTERS",
    "SEARCH_PLAN_REVISION",
    "PhraseRejection",
    "RejectedPhrase",
    "SearchBatch",
    "SearchPhrase",
    "SearchPlan",
    "UnqueryableInstrument",
    "plan_search_phrases",
]

#: Ruleset identity recorded on every plan, so a change of policy is visible in
#: an operator's output rather than only in this file's history.
SEARCH_PLAN_REVISION = "news-search-phrases-v1"

#: Phrases per request.
#:
#: Deliberately conservative *towards the provider*. The only live evidence
#: DHRUVA has of GDELT's throttling is an HTTP 429 on a single anonymous
#: request, so the batch size that matters is the one that keeps the number of
#: requests small: eight phrases turns a twenty-instrument watchlist into single
#: figures of requests rather than dozens. It is not the largest possible batch
#: either -- one expression containing the whole watchlist is unreviewable, and
#: a single rate-limited response would then lose the entire pass.
DEFAULT_BATCH_SIZE = 8
MIN_BATCH_SIZE = 1
#: A query expression travels as a URL parameter. Beyond roughly this many
#: phrases the request stops being something a person can read back from a log.
MAX_BATCH_SIZE = 25

#: Phrases kept per instrument: the company name, and the short form the press
#: actually uses. A third spelling costs a share of a scarce request budget to
#: find articles the first two already found.
MAX_PHRASES_PER_INSTRUMENT = 2

#: A one-word phrase shorter than this is an abbreviation, not a name.
MIN_SINGLE_TOKEN_CHARACTERS = 4

MAX_PHRASE_CHARACTERS = 120

#: Suffixes the press drops. Stripped only when at least two tokens survive, so
#: "Eternal Limited" never becomes the adjective "eternal" while "Adani
#: Enterprises Limited" becomes the phrase headlines are actually written with.
_CORPORATE_SUFFIXES = ("limited", "ltd", "plc", "inc", "corporation", "corp")
_MIN_SHORTENED_NAME_TOKENS = 2

#: Characters that mean something structural in every provider query language
#: this could target. A phrase containing one is refused rather than escaped:
#: escaping rules differ per provider, and a phrase that needs escaping is a
#: phrase somebody should look at.
_RESERVED_CHARACTERS = ('"', "(", ")")

_WHITESPACE = re.compile(r"\s+")


class PhraseRejection(StrEnum):
    """Why one candidate phrase was refused."""

    #: Nothing survived normalisation.
    EMPTY = "EMPTY"
    #: Longer than a name; probably a description.
    TOO_LONG = "TOO_LONG"
    #: Contains a character that is an operator in provider query syntax.
    RESERVED_CHARACTER = "RESERVED_CHARACTER"
    #: A single token that is the exchange symbol, or shaped like one.
    SYMBOL_SHAPED = "SYMBOL_SHAPED"
    #: A single token too short to name a company unambiguously.
    TOO_SHORT = "TOO_SHORT"
    #: An earlier instrument, or an earlier candidate, already claimed it.
    DUPLICATE = "DUPLICATE"
    #: The instrument already has as many phrases as it is allowed.
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    #: A former name whose grace period has run out. The press has stopped
    #: using it, and a name reused later by somebody else must not inherit the
    #: mapping.
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class SearchPhrase:
    """One phrase to ask a source for, and the instrument it stands for."""

    text: str
    kind: MatchKind
    instrument_id: InstrumentId
    canonical_symbol: str

    def __post_init__(self) -> None:
        """Require a phrase a provider adapter can quote without escaping."""
        invariant(bool(self.text.strip()), "a search phrase must not be empty")
        invariant(self.text == self.text.strip(), "a search phrase must be normalised")
        invariant(
            not any(character in self.text for character in _RESERVED_CHARACTERS),
            "a search phrase must not contain provider query operators",
        )
        invariant(
            self.kind is not MatchKind.CANONICAL_SYMBOL,
            "an exchange symbol is never a search phrase",
        )


@dataclass(frozen=True, slots=True)
class RejectedPhrase:
    """A candidate that was not queried, and the rule that refused it."""

    text: str
    kind: MatchKind
    canonical_symbol: str
    reason: PhraseRejection


@dataclass(frozen=True, slots=True)
class UnqueryableInstrument:
    """An approved instrument for which no safe phrase could be built."""

    instrument_id: InstrumentId
    canonical_symbol: str
    reason: str


@dataclass(frozen=True, slots=True)
class SearchBatch:
    """One request's worth of phrases, in a fixed order."""

    index: int
    phrases: tuple[SearchPhrase, ...]

    def __post_init__(self) -> None:
        """Require a numbered, non-empty batch."""
        invariant(self.index >= 1, "batches are numbered from one")
        invariant(bool(self.phrases), "an empty batch would be a request for nothing")

    @property
    def texts(self) -> tuple[str, ...]:
        """Return just the phrase text, for an adapter to quote."""
        return tuple(phrase.text for phrase in self.phrases)

    @property
    def symbols(self) -> tuple[str, ...]:
        """Return the canonical symbols this batch is looking for."""
        return tuple(dict.fromkeys(phrase.canonical_symbol for phrase in self.phrases))


@dataclass(frozen=True, slots=True)
class SearchPlan:
    """Everything one polling pass intends to ask for, and what it gave up on."""

    batches: tuple[SearchBatch, ...]
    unqueryable: tuple[UnqueryableInstrument, ...]
    rejected: tuple[RejectedPhrase, ...]
    revision: str = SEARCH_PLAN_REVISION

    @property
    def phrase_count(self) -> int:
        """Return how many phrases will actually be asked for."""
        return sum(len(batch.phrases) for batch in self.batches)


def plan_search_phrases(
    instruments: Iterable[LinkableInstrument],
    *,
    on: date,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> SearchPlan:
    """Turn the approved universe into deterministic, bounded query batches.

    Parameters
    ----------
    instruments
        The approved active universe. Order is ignored; the plan sorts by
        canonical symbol so the same watchlist always produces the same requests.
    on
        The day the plan is for. A former name is only offered while it is still
        within its grace period (:data:`FORMER_NAME_GRACE`).
    batch_size
        Phrases per request.

    Raises
    ------
    ValidationError
        If ``batch_size`` is outside its bounds.
    """
    if not MIN_BATCH_SIZE <= batch_size <= MAX_BATCH_SIZE:
        raise ValidationError(
            "news search batch size is outside its permitted bounds",
            batch_size=batch_size,
            minimum=MIN_BATCH_SIZE,
            maximum=MAX_BATCH_SIZE,
        )

    accepted: list[SearchPhrase] = []
    rejected: list[RejectedPhrase] = []
    unqueryable: list[UnqueryableInstrument] = []
    claimed: set[str] = set()

    for instrument in sorted(instruments, key=lambda entry: entry.canonical_symbol):
        taken = _phrases_for(instrument, on=on, claimed=claimed, rejected=rejected)
        accepted.extend(taken)
        if not taken:
            unqueryable.append(
                UnqueryableInstrument(
                    instrument_id=instrument.instrument_id,
                    canonical_symbol=instrument.canonical_symbol,
                    reason="no company name, alias or former name survived the safety rules",
                )
            )

    batches = tuple(
        SearchBatch(index=number, phrases=tuple(accepted[start : start + batch_size]))
        for number, start in enumerate(range(0, len(accepted), batch_size), start=1)
    )
    return SearchPlan(
        batches=batches,
        unqueryable=tuple(unqueryable),
        rejected=tuple(rejected),
    )


def _phrases_for(
    instrument: LinkableInstrument,
    *,
    on: date,
    claimed: set[str],
    rejected: list[RejectedPhrase],
) -> tuple[SearchPhrase, ...]:
    """Return the phrases one instrument contributes, recording every refusal."""
    taken: list[SearchPhrase] = []
    for text, kind, eligible in _candidates(instrument, on=on):
        reason = (
            PhraseRejection.EXPIRED
            if not eligible
            else _refuse(
                text,
                instrument=instrument,
                claimed=claimed,
                budget_left=len(taken) < MAX_PHRASES_PER_INSTRUMENT,
            )
        )
        normalised = _normalise(text)
        if reason is not None:
            rejected.append(
                RejectedPhrase(
                    text=normalised or text,
                    kind=kind,
                    canonical_symbol=instrument.canonical_symbol,
                    reason=reason,
                )
            )
            continue
        claimed.add(normalised.casefold())
        taken.append(
            SearchPhrase(
                text=normalised,
                kind=kind,
                instrument_id=instrument.instrument_id,
                canonical_symbol=instrument.canonical_symbol,
            )
        )
    return tuple(taken)


def _candidates(
    instrument: LinkableInstrument,
    *,
    on: date,
) -> tuple[tuple[str, MatchKind, bool], ...]:
    """Return every candidate for one instrument, in the order they are tried.

    The company name first because it is the most specific thing the instrument
    has, then aliases and former names longest-first: a longer name is a narrower
    search, and narrowing beats broadening when the cost of a wrong link is a
    scan citing the wrong company.

    The third element of each candidate is whether it is still eligible on
    ``on``. An expired former name is returned rather than filtered out here, so
    that the caller can report it as refused instead of dropping it silently.
    """
    aliases = tuple(
        (text, MatchKind.ALIAS) for text in sorted(instrument.aliases, key=_specificity)
    )
    historical = tuple(
        (name.text, MatchKind.FORMER_NAME, name.eligible_on(on))
        for name in sorted(instrument.former_names, key=lambda entry: _specificity(entry.text))
    )
    return (
        (_shorten(instrument.company_name), MatchKind.COMPANY_NAME, True),
        *((text, kind, True) for text, kind in aliases),
        *historical,
    )


def _specificity(text: str) -> tuple[int, str]:
    """Sort key placing longer, then alphabetically earlier, names first."""
    return (-len(text), text.casefold())


def _shorten(company_name: str) -> str:
    """Drop a trailing corporate suffix when a usable name survives it."""
    tokens = _normalise(company_name).split(" ")
    surviving = len(tokens) - 1
    if surviving >= _MIN_SHORTENED_NAME_TOKENS and tokens[-1].casefold().strip(".") in (
        _CORPORATE_SUFFIXES
    ):
        return " ".join(tokens[:-1])
    return " ".join(tokens)


def _normalise(text: str) -> str:
    """Collapse whitespace so two spellings of one name compare equal."""
    return _WHITESPACE.sub(" ", text).strip()


def _refuse(
    text: str,
    *,
    instrument: LinkableInstrument,
    claimed: set[str],
    budget_left: bool,
) -> PhraseRejection | None:
    """Return why this candidate cannot be queried, or ``None`` if it can.

    Order matters: a candidate that is both unsafe and a duplicate is reported
    as unsafe, because that is the finding an operator needs to act on.
    """
    normalised = _normalise(text)
    unsafe = _unsafe(normalised, canonical_symbol=instrument.canonical_symbol)
    if unsafe is not None:
        return unsafe
    if normalised.casefold() in claimed:
        return PhraseRejection.DUPLICATE
    if not budget_left:
        return PhraseRejection.BUDGET_EXHAUSTED
    return None


def _unsafe(normalised: str, *, canonical_symbol: str) -> PhraseRejection | None:
    """Return why this text is not usable as a phrase at all."""
    if not normalised:
        return PhraseRejection.EMPTY
    if any(character in normalised for character in _RESERVED_CHARACTERS):
        return PhraseRejection.RESERVED_CHARACTER
    if len(normalised) > MAX_PHRASE_CHARACTERS:
        return PhraseRejection.TOO_LONG
    if _is_symbol_shaped(normalised, canonical_symbol=canonical_symbol):
        return PhraseRejection.SYMBOL_SHAPED
    if " " not in normalised and len(normalised) < MIN_SINGLE_TOKEN_CHARACTERS:
        return PhraseRejection.TOO_SHORT
    return None


def _is_symbol_shaped(text: str, *, canonical_symbol: str) -> bool:
    """Return whether this reads as a ticker rather than as a name.

    Only single-token candidates qualify. ``HDFC AMC`` is upper case throughout
    and is exactly how the press writes that company; ``SBI`` is upper case and
    is a prefix shared by three separately listed ones.

    The comparison against the symbol is deliberately case-*sensitive*. NSE
    symbols are uppercase by invariant, so ``INDIGO`` in an alias list is
    somebody entering the ticker, while ``IndiGo`` is the name on the aircraft.
    Folding case would refuse the brand along with the ticker and leave that
    airline searchable only as "InterGlobe Aviation", which is not a phrase any
    headline about it contains.
    """
    if " " in text:
        return False
    if text == canonical_symbol:
        return True
    return text.upper() == text and any(character.isalpha() for character in text)
