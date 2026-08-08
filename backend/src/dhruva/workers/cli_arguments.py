"""Argument parsing shared by the read-only operator commands.

``dhruva-digest`` and ``dhruva-export`` ask the archive the same question and
differ only in where the answer goes, so they must agree exactly on what an
account, a cutoff, a window and a bound mean. Two copies of these rules would
agree until the day one of them was fixed, and then a snapshot would describe a
different instant from the digest it was supposed to be a record of.

Every function here refuses rather than defaults. A cutoff that could not be
parsed becomes an error, not "now"; an unknown symbol becomes an error, not an
empty report about an instrument DHRUVA does not follow.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from dhruva.contexts.intelligence.application.watchlist_digest import instruments_by_symbol
from dhruva.contexts.marketdata.api import MAX_MULTI_DAY_SESSIONS
from dhruva.shared.errors import DhruvaError, ValidationError
from dhruva.shared.identity import AccountId
from dhruva.shared.time.clock import SystemClock

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dhruva.contexts.intelligence.domain.entity_linking import LinkableInstrument

__all__ = [
    "BRIEF_TOP_CEILING",
    "BRIEF_TOP_DEFAULT",
    "MAX_ITEMS_CEILING",
    "parse_account",
    "parse_change_window",
    "parse_cutoff",
    "parse_max_items",
    "parse_sessions",
    "parse_top",
    "parse_window",
    "select_instruments",
    "validate_brief_options",
    "validate_changes_options",
]

#: Items one section may show. A digest is something a person reads; past this
#: it is a dump, and a dump is what the export exists for.
MAX_ITEMS_CEILING = 50

#: Instruments a brief shows by default. Small enough to read in one glance;
#: an owner who wants more says so explicitly with --top.
BRIEF_TOP_DEFAULT = 5

#: The largest --top a brief accepts. A brief exists to be short; past this
#: many entries it is the digest again, and --ranked already reports all of
#: them.
BRIEF_TOP_CEILING = 20

#: A stable label an account may be named by. Bounded and lowercase so that two
#: spellings of one intent cannot become two accounts, which is the failure this
#: whole mechanism exists to avoid.
_ACCOUNT_LABEL = re.compile(r"[a-z][a-z0-9-]{1,63}")


def parse_account(raw: str) -> AccountId:
    """Resolve an account from an identifier or from a stable label.

    Two accepted forms, because the repository has no canonical owner account
    and inventing one here would be worse than accepting both:

    * an existing identifier, ``acct_<uuid>`` or a bare UUID, which parses
      straight through;
    * a stable label such as ``owner-family``, from which the identifier is
      *derived* -- the same label always yields the same account, in every
      process and every run.

    The label form exists because of a specific, silent failure. Every command
    here requires ``--account``, and seeding the watchlist under one identifier
    and then reading it under another produces an empty digest that looks like a
    bug in the digest. A label the owner can remember and retype is far harder
    to get wrong than a UUID they have to keep somewhere.

    Raises
    ------
    ValidationError
        If the text is neither a valid identifier nor a usable label. Refusing
        is deliberate: silently minting an account for a typo would create a
        second, empty watchlist and hide the mistake.
    """
    try:
        return AccountId.parse(raw)
    except DhruvaError:
        pass
    if _ACCOUNT_LABEL.fullmatch(raw):
        return AccountId.deterministic(raw)
    raise ValidationError(
        "--account must be an account identifier (acct_<uuid> or a bare UUID) "
        "or a stable lowercase label such as 'owner-family'",
        supplied=raw,
    )


def parse_cutoff(raw: str | None) -> datetime:
    """Parse an explicit ISO-8601 cutoff, defaulting to now in UTC.

    A value with no zone is read as UTC and one in another zone is converted,
    because every stored instant is UTC (ADR-006) and a cutoff that meant
    something else would silently answer a different question.
    """
    if raw is None:
        return SystemClock().now()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as error:
        raise ValidationError("--as-of is not an ISO-8601 timestamp", value=raw) from error
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def parse_window(days: int | None, fallback: int) -> timedelta:
    """Return the publication window, refusing a non-positive one."""
    chosen = fallback if days is None else days
    if chosen < 1:
        raise ValidationError("--days must be a positive number of days", days=chosen)
    return timedelta(days=chosen)


def parse_sessions(requested: int) -> int:
    """Bound the multi-day return, refusing a window nothing could support."""
    if not 1 <= requested <= MAX_MULTI_DAY_SESSIONS:
        raise ValidationError(
            "--sessions must be between 1 and the multi-day ceiling",
            requested=requested,
            maximum=MAX_MULTI_DAY_SESSIONS,
        )
    return requested


def parse_max_items(requested: int) -> int:
    """Bound what one instrument contributes, refusing values that are not a report."""
    if not 1 <= requested <= MAX_ITEMS_CEILING:
        raise ValidationError(
            "--max-items must be between 1 and the ceiling",
            requested=requested,
            maximum=MAX_ITEMS_CEILING,
        )
    return requested


def parse_top(requested: int) -> int:
    """Bound how many instruments a brief may show, refusing an unreadable one."""
    if not 1 <= requested <= BRIEF_TOP_CEILING:
        raise ValidationError(
            "--top must be between 1 and the ceiling",
            requested=requested,
            maximum=BRIEF_TOP_CEILING,
        )
    return requested


def parse_change_window(from_raw: str, to_raw: str) -> tuple[datetime, datetime]:
    """Parse and order-check the two cutoffs a change report compares.

    ``--to`` earlier than ``--from`` cannot be answered: a change report
    reads forward from the earlier cutoff to the later one, and silently
    swapping them would compare in a direction the operator did not ask for.
    Equal cutoffs are accepted -- the report over them is empty, and empty is
    a complete, correct answer rather than a special case.
    """
    from_cutoff = parse_cutoff(from_raw)
    to_cutoff = parse_cutoff(to_raw)
    if to_cutoff < from_cutoff:
        raise ValidationError(
            "--to must not be earlier than --from",
            from_=from_cutoff.isoformat(),
            to=to_cutoff.isoformat(),
        )
    return from_cutoff, to_cutoff


def validate_changes_options(  # noqa: PLR0913 - one keyword per independently varied flag
    *,
    changes: bool,
    ranked: bool,
    brief: bool,
    as_of: str | None,
    from_cutoff: str | None,
    to_cutoff: str | None,
) -> None:
    """Refuse an option combination --changes and the other renderings cannot both satisfy.

    A change report replaces the single-cutoff renderings entirely -- it
    reads the archive twice, once per cutoff, rather than once -- so it is
    refused alongside ``--ranked``/``--brief`` rather than silently choosing
    one shape to print. ``--as-of`` names one cutoff; ``--changes`` names two
    explicitly, so accepting both would leave one silently unused.
    """
    if changes and (ranked or brief):
        raise ValidationError("--changes is mutually exclusive with --ranked and --brief")
    if changes and as_of is not None:
        raise ValidationError("--changes compares --from and --to; --as-of does not apply")
    if changes and (from_cutoff is None or to_cutoff is None):
        raise ValidationError("--changes requires both --from and --to")
    if not changes and (from_cutoff is not None or to_cutoff is not None):
        raise ValidationError("--from and --to are only meaningful together with --changes")


def validate_brief_options(*, brief: bool, ranked: bool, top: int | None) -> None:
    """Refuse an option combination --brief and --ranked cannot both satisfy.

    ``--top`` bounds a brief's own selection; asking for it without ``--brief``
    would silently do nothing rather than the "past a point it is a dump"
    ceiling it looks like. ``--brief`` and ``--ranked`` render two different,
    incompatible shapes of the same ranking -- one full digest with the
    ranking ahead of it, one compact substitute for the digest entirely -- so
    combining them is refused rather than picking one silently.
    """
    if top is not None and not brief:
        raise ValidationError("--top is only meaningful together with --brief")
    if brief and ranked:
        raise ValidationError("--brief and --ranked are mutually exclusive")


def select_instruments(
    universe: tuple[LinkableInstrument, ...],
    symbols: Sequence[str] | None,
) -> tuple[LinkableInstrument, ...]:
    """Narrow the universe, refusing a symbol that is not on the watchlist.

    Refusing matters more here than it looks: a mistyped symbol would otherwise
    produce a confident, empty, entirely truthful-looking report about an
    instrument DHRUVA does not follow — and in an exported snapshot that report
    outlives the mistake.
    """
    if not symbols:
        return universe
    chosen = instruments_by_symbol(universe, symbols)
    found = {entry.canonical_symbol.upper() for entry in chosen}
    missing = sorted({symbol.upper() for symbol in symbols} - found)
    if missing:
        known = ", ".join(sorted(entry.canonical_symbol for entry in universe)) or "none"
        raise ValidationError(
            f"not on the approved watchlist at this cutoff: {', '.join(missing)}. "
            f"Known symbols: {known}"
        )
    return chosen
