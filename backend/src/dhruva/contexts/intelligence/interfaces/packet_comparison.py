"""Compare two saved ``dhruva.research-packet.v1`` files, and only those files.

Answers a narrower, file-scoped version of the question
:mod:`~dhruva.contexts.intelligence.domain.changes` answers for two live
database reads: "what changed between these two already-exported research
states?" Nothing here opens a database connection, contacts a provider, or
recomputes anything from data newer than either file -- the two packets are
the entire, immutable source of truth, which is what lets a comparison
written down today still mean the same thing years later.

**Untrusted input, validated strictly.** :func:`read_packet` takes raw bytes
that may not even be JSON, and returns either a small, intentional
:class:`ValidatedPacket` DTO or a :class:`~dhruva.shared.errors.ValidationError`
naming exactly what was wrong. It never reconstructs a domain object
(``ResearchAttention``, ``MarketContext``, ``ArchivedNewsItem``) from the
file -- a packet is an external artifact, not a serialised domain object, and
treating it as one would let a hand-edited file smuggle an invalid domain
state past every invariant the domain layer enforces at construction. Every
field this module reads is a primitive (``str``/``int``/``bool``) or an
opaque ``Mapping`` compared only for equality, never re-interpreted.

**The top-N limitation, made explicit.** A packet's ``attention`` array holds
only its top-N noteworthy instruments (:func:`~dhruva.contexts.intelligence.
domain.attention.top_attention`'s own policy), not the whole watchlist. An
instrument absent from one side's array might have scored exactly zero, or
might have scored positively but been crowded out of the top-N -- the file
alone cannot always tell these apart. :attr:`ValidatedPacket.is_complete`
answers the one question that lets the difference be told:
``watchlist_summary.with_attention`` (the *true* count of score-positive
instruments in the whole watchlist, recorded independently of ``top_n``)
equals the length of the array actually exported. Only when that holds does
this module report a true attention entry/exit; otherwise it reports the
weaker, honest claim that the instrument entered or left *this packet's
selection*, which is exactly what the two files can prove.

**The same limitation applies to news.** An instrument's news is compared
(``NEWS_ADDED``/``NEWS_REMOVED``) only when it has an attention entry on
*both* sides to diff. An instrument entering or leaving attention has no
comparable news list on its absent side -- absence there means "scored zero,
not exported," never "exported with zero news" -- so archived news attached
to that transition is shown in the rendered output, but never claimed as
newly added or removed. See :func:`_compare_entry`'s docstring for the full
reasoning.

**Market facts, not the whole stored dict.** A packet's ``market`` summary
carries a few fields whose value is a function of the packet's own ``as_of``
cutoff (``as_of`` itself, ``staleness_days``, ``is_stale``,
``stale_after_days``) rather than of the market. Comparing the raw dict would
report ``MARKET_CHANGED`` on every later re-export of an unchanged instrument
purely because time passed between exports; :func:`_market_fingerprint`
projects to the substantive fields only (close, date, return figures, volume
ratio, availability, bar count, known limitation).

**Reused, not reinvented.** The envelope/body split, the ``body_sha256``
fingerprint, and the canonical JSON serialisation are :mod:`digest_export`'s
own functions, imported rather than copied -- a comparison export is a third
artifact shape built the same way the snapshot and the packet already are.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from dhruva.contexts.intelligence.interfaces.attention_presentation import ATTENTION_DISCLAIMER
from dhruva.contexts.intelligence.interfaces.digest_export import body_fingerprint
from dhruva.contexts.intelligence.interfaces.news_presentation import NSE_UNAVAILABLE_NOTICE
from dhruva.contexts.intelligence.interfaces.research_packet import PACKET_SCHEMA_VERSION
from dhruva.shared.errors import ValidationError
from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "COMPARISON_SCHEMA_VERSION",
    "PacketAttentionEntry",
    "PacketChange",
    "PacketChangeCategory",
    "ValidatedPacket",
    "build_comparison_export",
    "compare_packets",
    "read_packet",
    "render_packet_comparison",
    "validate_packet_pair",
]

#: A third artifact shape, independent of ``dhruva.research-snapshot.v1`` and
#: ``dhruva.research-packet.v1`` -- a comparison is neither of those, and
#: giving it its own version lets a consumer refuse a file it does not
#: understand instead of misreading one.
COMPARISON_SCHEMA_VERSION = "dhruva.research-packet-comparison.v1"

_INDENT = "    "
_RULE = "-" * 72


class PacketChangeCategory(StrEnum):
    """One deterministic, mechanically-derived kind of difference between two packets.

    ``ENTERED_ATTENTION``/``EXITED_ATTENTION`` are the strong, provable claim:
    the instrument's score crossed zero. ``ENTERED_PACKET_SELECTION``/
    ``LEFT_PACKET_SELECTION`` are the weaker claim a truncated packet can
    still support: the instrument's presence in *this file* changed, with no
    claim about its actual score on the side where it is absent.
    """

    #: Score was provably 0 in the earlier packet and is positive in the later
    #: one -- provable because the earlier packet's array was complete.
    ENTERED_ATTENTION = "ENTERED_ATTENTION"
    #: Score was positive in the earlier packet and is provably 0 in the
    #: later one -- provable because the later packet's array was complete.
    EXITED_ATTENTION = "EXITED_ATTENTION"
    #: Present in the later packet, absent from the earlier one, but the
    #: earlier packet's array was truncated -- absence there does not prove
    #: the score was zero.
    ENTERED_PACKET_SELECTION = "ENTERED_PACKET_SELECTION"
    #: Present in the earlier packet, absent from the later one, but the
    #: later packet's array was truncated -- absence there does not prove
    #: the score is now zero.
    LEFT_PACKET_SELECTION = "LEFT_PACKET_SELECTION"
    SCORE_INCREASED = "SCORE_INCREASED"
    SCORE_DECREASED = "SCORE_DECREASED"
    BAND_CHANGED = "BAND_CHANGED"
    REASONS_CHANGED = "REASONS_CHANGED"
    #: A difference in the stored market facts that describe the market
    #: itself -- close, date, return figures, volume ratio, availability, bar
    #: count, and known data-limitation note (see ``_MARKET_FACT_FIELDS``).
    #: Deliberately excludes fields whose value is a function of the packet's
    #: own ``as_of`` cutoff rather than of the market (``as_of`` itself,
    #: ``staleness_days``, ``is_stale``, ``stale_after_days``): comparing
    #: those would report every later re-export of an unchanged instrument as
    #: a market change purely because time passed between exports.
    MARKET_CHANGED = "MARKET_CHANGED"
    #: An archived item (by ``provider_item_id``) present in both packets'
    #: news lists for one instrument, absent from the earlier one's. Reported
    #: only when both sides have an attention entry to diff -- an
    #: ENTERED_ATTENTION/ENTERED_PACKET_SELECTION transition never carries
    #: this category, because there is no earlier news list to diff against
    #: (see :func:`_compare_entry`'s docstring for why that is not the same
    #: as an empty one).
    NEWS_ADDED = "NEWS_ADDED"
    #: The structural counterpart of ``NEWS_ADDED`` -- unusual under ordinary
    #: PIT progression (the archive is append-only), supported here because a
    #: hand-edited or narrower re-export is not this module's business to
    #: rule out. Reported only when both sides have an attention entry, for
    #: the identical reason ``NEWS_ADDED`` is.
    NEWS_REMOVED = "NEWS_REMOVED"


#: Fixed report order -- set-membership claims first (strong, then weak),
#: then magnitude, then supporting detail -- so two runs over the same two
#: files print categories in the same order rather than detection order.
_CATEGORY_ORDER = (
    PacketChangeCategory.ENTERED_ATTENTION,
    PacketChangeCategory.EXITED_ATTENTION,
    PacketChangeCategory.ENTERED_PACKET_SELECTION,
    PacketChangeCategory.LEFT_PACKET_SELECTION,
    PacketChangeCategory.SCORE_INCREASED,
    PacketChangeCategory.SCORE_DECREASED,
    PacketChangeCategory.BAND_CHANGED,
    PacketChangeCategory.REASONS_CHANGED,
    PacketChangeCategory.MARKET_CHANGED,
    PacketChangeCategory.NEWS_ADDED,
    PacketChangeCategory.NEWS_REMOVED,
)


@dataclass(frozen=True, slots=True)
class PacketAttentionEntry:
    """One instrument's already-computed verdict, read back from a packet file.

    ``market`` and each entry of ``news_by_id`` are kept as opaque, validated-
    enough mappings rather than re-parsed into domain types -- comparison
    only ever needs whole-value equality or a few named fields for display,
    never re-interpretation of what a close or a headline means.
    """

    instrument_id: str
    canonical_symbol: str
    company_name: str
    score: int
    band: str
    reasons: tuple[str, ...]
    market_context_available: bool
    market: Mapping[str, Any]
    #: ``provider_item_id`` per archived item, in the packet's own order.
    news_ids: tuple[str, ...]
    news_by_id: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class ValidatedPacket:
    """A packet file's contents, validated and reduced to what comparison needs."""

    account_id: str
    as_of: datetime
    top_n: int
    total_ranked: int
    with_attention: int
    attention_by_symbol: Mapping[str, PacketAttentionEntry]
    #: Canonical symbols in the packet's own stored order.
    attention_order: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        """Return whether the array holds every score-positive instrument, not just top-N.

        True exactly when nothing was truncated:
        :func:`~dhruva.contexts.intelligence.domain.attention.top_attention`
        returns ``min(with_attention, top_n)`` entries, so the array's length
        equals ``with_attention`` if and only if every score-positive
        instrument fit inside ``top_n``.
        """
        return len(self.attention_order) == self.with_attention


@dataclass(frozen=True, slots=True)
class PacketChange:
    """What differs about one instrument between two packet files.

    ``before``/``after`` are ``None`` exactly when the instrument is absent
    from that packet's ``attention`` array -- never a fabricated zero-score
    entry, so a reader can tell "known absent" from "known present with
    nothing to report."
    """

    canonical_symbol: str
    company_name: str
    categories: tuple[PacketChangeCategory, ...]
    before: PacketAttentionEntry | None
    after: PacketAttentionEntry | None
    new_reasons: tuple[str, ...] = ()
    new_news: tuple[Mapping[str, Any], ...] = ()
    removed_news: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        """Require at least one category -- an unchanged instrument is not reported."""
        invariant(len(self.categories) > 0, "a packet change must name at least one category")


# --------------------------------------------------------------------------- #
# Reading and validating an untrusted file
# --------------------------------------------------------------------------- #


def read_packet(data: bytes) -> ValidatedPacket:
    """Parse, validate and reduce one packet file to a :class:`ValidatedPacket`.

    Fails closed on anything unexpected: invalid UTF-8, invalid JSON, a
    schema version this module does not read, a body whose fingerprint does
    not match its own envelope, or a required field that is missing or the
    wrong shape. Every failure raises :class:`~dhruva.shared.errors.
    ValidationError` naming what was wrong, never a best-effort partial read.

    Raises
    ------
    ValidationError
        If the file cannot be safely read as a valid ``PACKET_SCHEMA_VERSION``
        packet.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValidationError("packet file is not valid UTF-8") from error
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValidationError("packet file is not valid JSON", detail=str(error)) from error

    if not isinstance(raw, dict):
        raise ValidationError("packet file does not contain a JSON object")
    envelope = raw.get("envelope")
    body = raw.get("body")
    if not isinstance(envelope, dict) or not isinstance(body, dict):
        raise ValidationError("packet file is missing its envelope or body")

    schema_version = body.get("schema_version")
    if schema_version != PACKET_SCHEMA_VERSION:
        raise ValidationError(
            "packet has an unexpected schema_version -- this command reads "
            f"{PACKET_SCHEMA_VERSION} packets only",
            found=repr(schema_version),
        )

    expected_sha = envelope.get("body_sha256")
    if expected_sha != body_fingerprint(body):
        raise ValidationError(
            "packet body does not match its own recorded fingerprint -- the "
            "file may have been edited after it was exported"
        )

    account_id = _required_str(body, "account_id")
    as_of = _required_datetime(body, "as_of")
    top_n = _required_int(body, "top_n")
    total_ranked = _required_int(body, "total_ranked")

    summary = body.get("watchlist_summary")
    if not isinstance(summary, dict):
        raise ValidationError("packet is missing watchlist_summary")
    with_attention = _required_int(summary, "with_attention", where="watchlist_summary")

    revisions = body.get("revisions")
    if not isinstance(revisions, dict) or not revisions.get("attention"):
        raise ValidationError("packet is missing its attention model revision")

    attention_raw = body.get("attention")
    if not isinstance(attention_raw, list):
        raise ValidationError("packet is missing its attention array")

    order: list[str] = []
    by_symbol: dict[str, PacketAttentionEntry] = {}
    for index, item in enumerate(attention_raw):
        entry = _read_entry(item, index=index)
        if entry.canonical_symbol in by_symbol:
            raise ValidationError(
                "packet lists the same instrument twice in its attention array",
                canonical_symbol=entry.canonical_symbol,
            )
        order.append(entry.canonical_symbol)
        by_symbol[entry.canonical_symbol] = entry

    return ValidatedPacket(
        account_id=account_id,
        as_of=as_of,
        top_n=top_n,
        total_ranked=total_ranked,
        with_attention=with_attention,
        attention_by_symbol=by_symbol,
        attention_order=tuple(order),
    )


def _read_entry(item: Any, *, index: int) -> PacketAttentionEntry:
    """Validate and reduce one ``attention[]`` element."""
    where = f"attention[{index}]"
    if not isinstance(item, dict):
        raise ValidationError(f"{where} is not a JSON object")
    instrument_id = _required_str(item, "instrument_id", where=where)
    canonical_symbol = _required_str(item, "canonical_symbol", where=where)
    company_name = _required_str(item, "company_name", where=where)
    score = _required_int(item, "score", where=where)
    band = _required_str(item, "band", where=where)

    reasons_raw = item.get("reasons")
    if not isinstance(reasons_raw, list) or not all(isinstance(r, str) for r in reasons_raw):
        raise ValidationError(f"{where} reasons must be a list of strings")

    market_context_available = item.get("market_context_available")
    if not isinstance(market_context_available, bool):
        raise ValidationError(f"{where} is missing market_context_available")

    market = item.get("market")
    if not isinstance(market, dict):
        raise ValidationError(f"{where} is missing its market summary")

    news_raw = item.get("news")
    if not isinstance(news_raw, list):
        raise ValidationError(f"{where} is missing its news array")

    news_ids: list[str] = []
    news_by_id: dict[str, Mapping[str, Any]] = {}
    for news_index, news_item in enumerate(news_raw):
        if not isinstance(news_item, dict):
            raise ValidationError(f"{where} news[{news_index}] is not a JSON object")
        provider_item_id = news_item.get("provider_item_id")
        if not isinstance(provider_item_id, str) or not provider_item_id:
            raise ValidationError(f"{where} news[{news_index}] is missing provider_item_id")
        news_ids.append(provider_item_id)
        news_by_id[provider_item_id] = news_item

    return PacketAttentionEntry(
        instrument_id=instrument_id,
        canonical_symbol=canonical_symbol,
        company_name=company_name,
        score=score,
        band=band,
        reasons=tuple(reasons_raw),
        market_context_available=market_context_available,
        market=market,
        news_ids=tuple(news_ids),
        news_by_id=news_by_id,
    )


def _required_str(payload: Mapping[str, Any], field_name: str, *, where: str = "packet") -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{where} is missing required field {field_name!r}")
    return value


def _required_int(payload: Mapping[str, Any], field_name: str, *, where: str = "packet") -> int:
    value = payload.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{where} is missing required integer field {field_name!r}")
    return value


def _required_datetime(payload: Mapping[str, Any], field_name: str) -> datetime:
    raw = _required_str(payload, field_name)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as error:
        raise ValidationError(
            f"packet field {field_name!r} is not an ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None:
        raise ValidationError(f"packet field {field_name!r} is not timezone-aware")
    return parsed.astimezone(UTC)


# --------------------------------------------------------------------------- #
# Cross-packet validation
# --------------------------------------------------------------------------- #


def validate_packet_pair(before: ValidatedPacket, after: ValidatedPacket) -> None:
    """Refuse a pair of packets this module cannot compare.

    Raises
    ------
    ValidationError
        If the two packets are for different accounts, or ``after`` claims
        knowledge as of an instant earlier than ``before`` -- a comparison
        reads forward from the earlier cutoff to the later one, and silently
        swapping them would compare in a direction the caller did not ask
        for, exactly as :func:`~dhruva.workers.cli_arguments.
        parse_change_window` refuses for the database-backed comparison.
    """
    if before.account_id != after.account_id:
        raise ValidationError(
            "--from and --to packets are for different accounts",
            from_account=before.account_id,
            to_account=after.account_id,
        )
    if after.as_of < before.as_of:
        raise ValidationError(
            "--to packet's as_of must not be earlier than --from packet's as_of",
            from_as_of=before.as_of.isoformat(),
            to_as_of=after.as_of.isoformat(),
        )


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #

#: The stored ``market`` fields that describe the market itself, in the exact
#: shape :func:`~dhruva.contexts.intelligence.interfaces.digest_export.
#: market_summary` writes them. Deliberately excludes ``as_of``,
#: ``staleness_days``, ``is_stale`` and ``stale_after_days``: all four are a
#: function of the packet's own point-in-time cutoff (elapsed time since
#: ``latest_date``), not of the market, and grow or flip on every later
#: re-export even when no new bar was ever ingested. Comparing the whole dict
#: would report every such re-export as MARKET_CHANGED.
_MARKET_FACT_FIELDS = (
    "requested",
    "availability",
    "latest_date",
    "latest_close",
    "previous_close",
    "one_day_change_percent",
    "multi_day_change_percent",
    "multi_day_sessions",
    "latest_volume",
    "volume_ratio",
    "volume_baseline_sessions",
    "bars_available",
    "limitation",
    "revision",
)


def _market_fingerprint(market: Mapping[str, Any]) -> tuple[Any, ...]:
    """Project a packet's stored ``market`` summary to only its substantive facts.

    See :data:`_MARKET_FACT_FIELDS` for exactly what is compared and why the
    rest is excluded. A field this module has never heard of is not read --
    the projection is an explicit allow-list, not everything-but-the-known-
    cutoff-fields, so a future packet field defaults to *not* triggering
    MARKET_CHANGED until this list is deliberately extended.
    """
    return tuple(market.get(field) for field in _MARKET_FACT_FIELDS)


def compare_packets(before: ValidatedPacket, after: ValidatedPacket) -> tuple[PacketChange, ...]:
    """Return one change per instrument mentioned in either packet, alphabetically.

    Ordered by canonical symbol rather than by size of score movement (unlike
    :func:`~dhruva.contexts.intelligence.domain.changes.compare_watchlist`):
    an instrument present in only one packet has no score on the other side
    to measure a movement against, so a magnitude ordering cannot always be
    formed here.
    """
    symbols = sorted(set(before.attention_by_symbol) | set(after.attention_by_symbol))
    changes: list[PacketChange] = []
    for symbol in symbols:
        change = _compare_entry(
            symbol,
            before.attention_by_symbol.get(symbol),
            after.attention_by_symbol.get(symbol),
            before_complete=before.is_complete,
            after_complete=after.is_complete,
        )
        if change is not None:
            changes.append(change)
    return tuple(changes)


def _compare_entry(
    symbol: str,
    before_entry: PacketAttentionEntry | None,
    after_entry: PacketAttentionEntry | None,
    *,
    before_complete: bool,
    after_complete: bool,
) -> PacketChange | None:
    """Compare one instrument's presence and, where present on both sides, its verdict.

    News is diffed by ``provider_item_id`` only when *both* sides have an
    attention entry to diff. When one side is absent (an ENTERED_ATTENTION,
    ENTERED_PACKET_SELECTION, EXITED_ATTENTION or LEFT_PACKET_SELECTION
    transition), no ``NEWS_ADDED``/``NEWS_REMOVED`` is reported, deliberately:
    an absent entry means "this instrument scored zero and was not exported,"
    not "this instrument was exported with an empty news list." Packet v1
    never records a zero-score instrument's news count, so treating the
    absent side as an empty news list would be inventing a fact the file does
    not contain -- exactly the kind of interpretation this module's own
    module docstring says it never performs. (The attention scoring formula
    happens to guarantee a zero score implies zero news for the one revision
    this codebase currently has, but relying on that would tie this file to
    scoring internals it explicitly does not read; a packet recording a
    future, differently-weighted revision would silently break the
    assumption. ``is_complete`` proves the *attention selection* is complete,
    never the *news list* of an instrument that was never in it.) The
    instrument's own news, still visible via ``before``/``after`` on the
    returned :class:`PacketChange`, is rendered separately and labelled as
    such rather than silently omitted -- see :func:`_unclaimed_news`.
    """
    found: set[PacketChangeCategory] = set()
    new_reasons: tuple[str, ...] = ()
    new_news: tuple[Mapping[str, Any], ...] = ()
    removed_news: tuple[Mapping[str, Any], ...] = ()

    if before_entry is None and after_entry is not None:
        found.add(
            PacketChangeCategory.ENTERED_ATTENTION
            if before_complete
            else PacketChangeCategory.ENTERED_PACKET_SELECTION
        )
    elif before_entry is not None and after_entry is None:
        found.add(
            PacketChangeCategory.EXITED_ATTENTION
            if after_complete
            else PacketChangeCategory.LEFT_PACKET_SELECTION
        )
    elif before_entry is not None and after_entry is not None:
        if after_entry.score > before_entry.score:
            found.add(PacketChangeCategory.SCORE_INCREASED)
        if after_entry.score < before_entry.score:
            found.add(PacketChangeCategory.SCORE_DECREASED)
        if after_entry.band != before_entry.band:
            found.add(PacketChangeCategory.BAND_CHANGED)
        if after_entry.reasons != before_entry.reasons:
            found.add(PacketChangeCategory.REASONS_CHANGED)
            new_reasons = tuple(r for r in after_entry.reasons if r not in before_entry.reasons)
        if _market_fingerprint(after_entry.market) != _market_fingerprint(before_entry.market):
            found.add(PacketChangeCategory.MARKET_CHANGED)

        before_ids = set(before_entry.news_ids)
        after_ids = set(after_entry.news_ids)
        added_ids = [i for i in after_entry.news_ids if i not in before_ids]
        removed_ids = [i for i in before_entry.news_ids if i not in after_ids]
        if added_ids:
            found.add(PacketChangeCategory.NEWS_ADDED)
            new_news = tuple(after_entry.news_by_id[i] for i in added_ids)
        if removed_ids:
            found.add(PacketChangeCategory.NEWS_REMOVED)
            removed_news = tuple(before_entry.news_by_id[i] for i in removed_ids)

    ordered = tuple(category for category in _CATEGORY_ORDER if category in found)
    if not ordered:
        return None

    company_name = (after_entry or before_entry).company_name  # type: ignore[union-attr]
    return PacketChange(
        canonical_symbol=symbol,
        company_name=company_name,
        categories=ordered,
        before=before_entry,
        after=after_entry,
        new_reasons=new_reasons,
        new_news=new_news,
        removed_news=removed_news,
    )


# --------------------------------------------------------------------------- #
# Presentation
# --------------------------------------------------------------------------- #


def render_packet_comparison(
    changes: Sequence[PacketChange], *, before: ValidatedPacket, after: ValidatedPacket
) -> str:
    """Render one deterministic comparison between two packet files."""
    lines = [
        "Research packet comparison",
        f"from {before.as_of.isoformat()}{_selection_note(before)}",
        f"to   {after.as_of.isoformat()}{_selection_note(after)}",
        "",
        ATTENTION_DISCLAIMER,
        "",
        _RULE,
        "",
    ]
    if not changes:
        lines.append("No changes between these two packets.")
        return "\n".join(lines)

    for change in changes:
        lines.extend(_render_change(change))
        lines.append("")
    return "\n".join(lines).rstrip()


def _selection_note(packet: ValidatedPacket) -> str:
    """Note when a packet's own array is a truncated top-N, not the full attention set."""
    if packet.is_complete:
        return ""
    return f"  [top {packet.top_n} of {packet.with_attention} noteworthy shown]"


def _render_change(change: PacketChange) -> list[str]:
    lines = [f"{change.canonical_symbol}  --  {change.company_name}"]
    lines.append(f"{_INDENT}categories: {', '.join(change.categories)}")
    lines.append(f"{_INDENT}attention: {_attention_transition(change)}")
    if change.new_reasons:
        lines.append(f"{_INDENT}new reasons:")
        lines.extend(f"{_INDENT * 2}- {reason}" for reason in change.new_reasons)
    if change.new_news:
        lines.append(f"{_INDENT}new archived news:")
        lines.extend(_news_lines(change.new_news))
    if change.removed_news:
        lines.append(f"{_INDENT}archived news no longer present:")
        lines.extend(_news_lines(change.removed_news))
    unclaimed = _unclaimed_news(change)
    if unclaimed:
        lines.append(
            f"{_INDENT}archived news present (not claimed as newly added -- "
            "no comparable state exists for this instrument on the other side):"
        )
        lines.extend(_news_lines(unclaimed))
    return lines


def _unclaimed_news(change: PacketChange) -> tuple[Mapping[str, Any], ...]:
    """Return the present side's own news for a transition with no side to diff against.

    Populated only when exactly one of ``before``/``after`` is ``None`` -- an
    ENTERED_ATTENTION, ENTERED_PACKET_SELECTION, EXITED_ATTENTION or
    LEFT_PACKET_SELECTION change. Not reported as ``NEWS_ADDED``/
    ``NEWS_REMOVED`` (see :func:`_compare_entry`'s docstring for why), but not
    silently withheld from the rendered output either: an instrument entering
    or leaving attention with archived news attached is a fact both packets
    already record, just not one this module can call "new".
    """
    if change.after is None and change.before is not None:
        entry = change.before
    elif change.before is None and change.after is not None:
        entry = change.after
    else:
        return ()
    return tuple(entry.news_by_id[i] for i in entry.news_ids)


def _attention_transition(change: PacketChange) -> str:
    """State the before/after verdict, using "0" only where the file actually proves it."""
    if PacketChangeCategory.ENTERED_PACKET_SELECTION in change.categories:
        return f"not in this packet's selection -> {_entry_repr(change.after)}"
    if PacketChangeCategory.LEFT_PACKET_SELECTION in change.categories:
        return f"{_entry_repr(change.before)} -> not in this packet's selection"
    if PacketChangeCategory.ENTERED_ATTENTION in change.categories:
        return f"LOW 0 -> {_entry_repr(change.after)}"
    if PacketChangeCategory.EXITED_ATTENTION in change.categories:
        return f"{_entry_repr(change.before)} -> LOW 0"
    return f"{_entry_repr(change.before)} -> {_entry_repr(change.after)}"


def _entry_repr(entry: PacketAttentionEntry | None) -> str:
    if entry is None:
        return "(absent)"
    return f"{entry.band} {entry.score}"


def _news_lines(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in entries:
        source = item.get("source")
        source = source if isinstance(source, dict) else {}
        lines.append(
            f"{_INDENT * 2}- {item.get('event_category', 'UNKNOWN')}  "
            f"sentiment {item.get('sentiment', 'UNKNOWN')}  "
            f"published {item.get('published_at', '')}"
        )
        lines.append(f"{_INDENT * 3}{item.get('title', '')}")
        lines.append(f"{_INDENT * 3}{item.get('url', '')}")
        display_name = source.get("display_name", "")
        key = source.get("key", "")
        lines.append(f"{_INDENT * 3}source: {display_name} ({key})")
    return lines


# --------------------------------------------------------------------------- #
# Optional canonical JSON export
# --------------------------------------------------------------------------- #


def build_comparison_export(
    changes: Sequence[PacketChange],
    *,
    before: ValidatedPacket,
    after: ValidatedPacket,
    generated_at: datetime,
) -> dict[str, Any]:
    """Return the complete comparison export: a volatile envelope and a stable body.

    ``generated_at`` reaches only the envelope, exactly as
    :func:`~dhruva.contexts.intelligence.interfaces.digest_export.
    build_snapshot` and :func:`~dhruva.contexts.intelligence.interfaces.
    research_packet.build_packet` keep it out of their own bodies -- two
    comparisons of the same two files differ in exactly one field.
    """
    body = _comparison_body(changes, before=before, after=after)
    return {
        "envelope": {
            "schema_version": COMPARISON_SCHEMA_VERSION,
            "generated_at": generated_at.isoformat(),
            "body_sha256": body_fingerprint(body),
            "notice": NSE_UNAVAILABLE_NOTICE,
            "disclaimer": ATTENTION_DISCLAIMER,
        },
        "body": body,
    }


def _comparison_body(
    changes: Sequence[PacketChange], *, before: ValidatedPacket, after: ValidatedPacket
) -> dict[str, Any]:
    return {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "account_id": before.account_id,
        "from_as_of": before.as_of.isoformat(),
        "to_as_of": after.as_of.isoformat(),
        "from_top_n": before.top_n,
        "to_top_n": after.top_n,
        "from_total_ranked": before.total_ranked,
        "to_total_ranked": after.total_ranked,
        "from_attention_complete": before.is_complete,
        "to_attention_complete": after.is_complete,
        "changes": [_change_dict(change) for change in changes],
    }


def _change_dict(change: PacketChange) -> dict[str, Any]:
    return {
        "canonical_symbol": change.canonical_symbol,
        "company_name": change.company_name,
        "categories": list(change.categories),
        "before_score": None if change.before is None else change.before.score,
        "after_score": None if change.after is None else change.after.score,
        "before_band": None if change.before is None else change.before.band,
        "after_band": None if change.after is None else change.after.band,
        "new_reasons": list(change.new_reasons),
        "new_news": [dict(item) for item in change.new_news],
        "removed_news": [dict(item) for item in change.removed_news],
    }
