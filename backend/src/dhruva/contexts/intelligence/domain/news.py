"""Provider-neutral news identity, permitted text and explainable deduplication.

DHRUVA stores *metadata about* an article, never the article. The types here
carry a bounded title and an optional bounded snippet and nothing else, because
what a free source permits is a link and a headline, not a body. A reader who
wants the piece follows :attr:`NewsItemIdentity.canonical_url` to the publisher.

Two timestamps, never one. ``published_at`` is what the source claims;
``first_seen_at`` is when DHRUVA observed it. A backtest may only read items it
could have seen, and the second timestamp is the only thing that can answer that
-- a corrected article keeps its original publication time and would otherwise
leak backwards (ADR-007).

Deduplication is a set of ordered, bounded rules, each of which names itself in
its verdict. Nothing here scores similarity: a rule either fires with a reason a
person can check, or the item is distinct.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dhruva.shared.errors import ValidationError
from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

__all__ = [
    "NEWS_IDENTITY_REVISION",
    "DeduplicationDecision",
    "DeduplicationLedger",
    "DeduplicationRule",
    "NewsFingerprints",
    "NewsItem",
    "NewsItemIdentity",
    "NewsSource",
    "NewsSourceTier",
    "PermittedText",
    "canonical_url",
    "normalise_headline",
    "significant_tokens",
]

#: Identity and fingerprint rules recorded on every item this module builds.
#:
#: A stored fingerprint that names an older revision was produced by different
#: normalisation and must not be compared with a current one.
NEWS_IDENTITY_REVISION = "news-identity-v1"

#: Longest permitted snippet. Deliberately short: the licence question is not
#: "how much is fair" but "how little is enough to be useful", and a paragraph
#: is enough to tell a reader whether to open the link.
MAX_SNIPPET = 400
MAX_TITLE = 300
_MAX_URL = 2048
_MAX_PROVIDER_ITEM_ID = 200
_SOURCE_KEY = re.compile(r"[a-z][a-z0-9-]{1,63}\Z")
_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")

#: Query parameters that identify a referrer rather than a document. Two links
#: differing only in these are the same article shared twice, and treating them
#: as distinct is how one press release becomes eleven headlines.
_TRACKING_PARAMETERS = frozenset(
    {
        "cmpid",
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "ref",
        "referrer",
        "source",
        "utm_campaign",
        "utm_content",
        "utm_id",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)

#: Words carrying no distinguishing information in a headline. Used only to
#: decide whether a rewritten headline has enough substance to compare.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "over",
        "the",
        "to",
        "with",
    }
)

#: Fewest significant tokens before the rewritten-headline rule may be used.
#:
#: Short headlines share their few words by coincidence -- "Q1 results out" is
#: not evidence of anything. Below this bound the rule declines to answer rather
#: than guessing, which is the difference between bounded and fuzzy.
MIN_REWRITE_TOKENS = 6


class NewsSourceTier(StrEnum):
    """How much weight an item's origin earns before anything is read.

    Credibility is a property of the source, recorded once, and never inferred
    from the text. A filing on the exchange's own feed is a different kind of
    fact from an aggregator's summary of somebody's blog.
    """

    OFFICIAL_FILING = "OFFICIAL_FILING"
    EXCHANGE_NOTICE = "EXCHANGE_NOTICE"
    ESTABLISHED_PUBLISHER = "ESTABLISHED_PUBLISHER"
    AGGREGATOR = "AGGREGATOR"
    UNVERIFIED = "UNVERIFIED"


class DeduplicationRule(StrEnum):
    """The named rule that decided one item was a repeat of another."""

    PROVIDER_ITEM_ID = "PROVIDER_ITEM_ID"
    CANONICAL_URL = "CANONICAL_URL"
    REPEATED_FILING = "REPEATED_FILING"
    SYNDICATED_HEADLINE = "SYNDICATED_HEADLINE"
    REWRITTEN_HEADLINE = "REWRITTEN_HEADLINE"


def _bounded(value: str, *, field: str, maximum: int) -> None:
    """Require canonical, bounded text rather than silently trimming it."""
    invariant(bool(value), f"{field} must not be empty")
    invariant(value == value.strip(), f"{field} must not have surrounding whitespace")
    invariant(len(value) <= maximum, f"{field} is too long", maximum=maximum)


def _utc(value: datetime, *, field: str) -> None:
    """Require a timezone-aware UTC instant (ADR-006)."""
    invariant(value.tzinfo is not None and value.utcoffset() is not None, f"{field} must be aware")
    invariant(value.utcoffset() == timedelta(0), f"{field} must be UTC")


def canonical_url(raw: str) -> str:
    """Reduce a link to the document it points at.

    Lowercases the scheme and host, drops a default port and a leading ``www.``,
    removes the fragment, removes referrer parameters, orders what survives and
    strips a trailing slash. Query parameters that are not known trackers are
    kept: an unknown parameter may well select the document.

    Raises
    ------
    ValidationError
        If the link is not an absolute ``http`` or ``https`` URL.
    """
    if not raw or raw != raw.strip() or len(raw) > _MAX_URL:
        raise ValidationError("news URL is empty, padded or too long")
    parts = urlsplit(raw)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValidationError("news URL must be an absolute http(s) link")

    host = parts.hostname.lower().removeprefix("www.")
    default_port = {"http": 80, "https": 443}[parts.scheme.lower()]
    netloc = host if parts.port in (None, default_port) else f"{host}:{parts.port}"
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_PARAMETERS
        )
    )
    path = parts.path.rstrip("/") if parts.path != "/" else ""
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def normalise_headline(value: str) -> str:
    """Fold a headline to the form two spellings of it share.

    Case, punctuation and runs of whitespace differ between a wire feed and the
    site that republished it. Nothing else is removed, so two headlines that
    normalise alike really did say the same words in the same order.
    """
    folded = _PUNCTUATION.sub(" ", value.casefold())
    return _WHITESPACE.sub(" ", folded).strip()


def significant_tokens(value: str) -> tuple[str, ...]:
    """Return the ordered, de-duplicated non-stopword tokens of a headline."""
    seen: dict[str, None] = {}
    for token in normalise_headline(value).split():
        if token not in _STOPWORDS:
            seen.setdefault(token, None)
    return tuple(seen)


def _digest(*parts: str) -> str:
    """Hash normalised parts into one stable fingerprint."""
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class NewsSource:
    """One feed, its credibility tier and where attribution should point."""

    key: str
    display_name: str
    tier: NewsSourceTier
    homepage_url: str

    def __post_init__(self) -> None:
        """Require an identifiable source that a reader can be sent back to."""
        invariant(bool(_SOURCE_KEY.fullmatch(self.key)), "invalid news source key")
        _bounded(self.display_name, field="display_name", maximum=120)
        invariant(
            self.homepage_url == canonical_url(self.homepage_url),
            "source homepage must already be canonical",
        )


@dataclass(frozen=True, slots=True)
class NewsItemIdentity:
    """What the provider called the item, and where the item actually lives."""

    source_key: str
    provider_item_id: str
    url: str

    def __post_init__(self) -> None:
        """Require a provider identifier and an already-canonical link."""
        invariant(bool(_SOURCE_KEY.fullmatch(self.source_key)), "invalid news source key")
        _bounded(
            self.provider_item_id,
            field="provider_item_id",
            maximum=_MAX_PROVIDER_ITEM_ID,
        )
        invariant(self.url == canonical_url(self.url), "news URL must already be canonical")


@dataclass(frozen=True, slots=True)
class PermittedText:
    """The only article text DHRUVA keeps: a headline and a bounded snippet.

    The bounds are the point. A type that cannot hold a body cannot be talked
    into holding one later by a provider adapter in a hurry.
    """

    title: str
    snippet: str | None = None

    def __post_init__(self) -> None:
        """Refuse anything longer than a headline and a short extract."""
        _bounded(self.title, field="title", maximum=MAX_TITLE)
        if self.snippet is not None:
            _bounded(self.snippet, field="snippet", maximum=MAX_SNIPPET)


@dataclass(frozen=True, slots=True)
class NewsFingerprints:
    """The four deterministic keys deduplication compares.

    ``rewrite`` is ``None`` when the headline is too short for the rewritten-
    headline rule to justify itself. Absence is the rule declining to answer,
    not a match against nothing.
    """

    identity: str
    url: str
    headline: str
    rewrite: str | None
    revision: str = NEWS_IDENTITY_REVISION


@dataclass(frozen=True, slots=True)
class NewsItem:
    """One observed news item, with both of its timestamps and its fingerprints."""

    identity: NewsItemIdentity
    source: NewsSource
    text: PermittedText
    published_at: datetime
    first_seen_at: datetime

    def __post_init__(self) -> None:
        """Require an item that belongs to its source and could have been seen."""
        invariant(
            self.identity.source_key == self.source.key,
            "news item identity and source must agree",
        )
        _utc(self.published_at, field="published_at")
        _utc(self.first_seen_at, field="first_seen_at")
        invariant(
            self.first_seen_at >= self.published_at,
            "an item cannot be observed before it was published",
        )

    @property
    def fingerprints(self) -> NewsFingerprints:
        """Derive this item's deduplication keys deterministically."""
        tokens = significant_tokens(self.text.title)
        published_day = self.published_at.date().isoformat()
        return NewsFingerprints(
            identity=_digest(
                NEWS_IDENTITY_REVISION,
                self.identity.source_key,
                self.identity.provider_item_id,
            ),
            url=_digest(NEWS_IDENTITY_REVISION, self.identity.url),
            headline=_digest(
                NEWS_IDENTITY_REVISION,
                normalise_headline(self.text.title),
                published_day,
            ),
            rewrite=(
                _digest(NEWS_IDENTITY_REVISION, *sorted(tokens), published_day)
                if len(tokens) >= MIN_REWRITE_TOKENS
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class DeduplicationDecision:
    """Whether an item is a repeat, which rule said so, and of what."""

    is_duplicate: bool
    rule: DeduplicationRule | None
    original: NewsItemIdentity | None
    reason: str
    revision: str = NEWS_IDENTITY_REVISION

    def __post_init__(self) -> None:
        """Require a duplicate to name both its rule and what it repeats."""
        _bounded(self.reason, field="reason", maximum=240)
        named = (self.rule is None, self.original is None)
        invariant(
            named[0] == named[1],
            "a deduplication verdict names a rule and an original together",
        )
        invariant(
            self.is_duplicate == (self.rule is not None),
            "a duplicate must name the rule that decided it",
        )


class DeduplicationLedger:
    """Ordered, bounded duplicate detection over the items already observed.

    The rules are tried strongest first, and the first to fire is the answer, so
    a verdict always has exactly one explanation. Nothing computes a similarity
    score: an item either repeats an identifier, a link, a filing, a headline on
    the same day, or the same words on the same day -- or it is a new item.
    """

    __slots__ = ("_by_headline", "_by_identity", "_by_rewrite", "_by_url", "_filings")

    def __init__(self) -> None:
        """Start an empty ledger."""
        self._by_identity: dict[str, NewsItemIdentity] = {}
        self._by_url: dict[str, NewsItemIdentity] = {}
        self._by_headline: dict[str, NewsItemIdentity] = {}
        self._by_rewrite: dict[str, NewsItemIdentity] = {}
        self._filings: dict[tuple[str, str], NewsItemIdentity] = {}

    def observe(self, item: NewsItem) -> DeduplicationDecision:
        """Classify one item and, if it is new, remember it.

        Idempotent: observing the same item twice returns a duplicate verdict the
        second time and changes nothing, which is what makes a retried feed poll
        safe to run as often as it likes.
        """
        decision = self.classify(item)
        if not decision.is_duplicate:
            self._remember(item)
        return decision

    def classify(self, item: NewsItem) -> DeduplicationDecision:
        """Decide whether an item repeats one already observed, without recording it."""
        keys = item.fingerprints
        filing_key = (item.identity.source_key, keys.headline)

        original = self._by_identity.get(keys.identity)
        if original is not None:
            return _duplicate(
                DeduplicationRule.PROVIDER_ITEM_ID,
                original,
                "the source has already delivered this item identifier",
            )
        original = self._by_url.get(keys.url)
        if original is not None:
            return _duplicate(
                DeduplicationRule.CANONICAL_URL,
                original,
                "the canonical link is already held",
            )
        if item.source.tier is NewsSourceTier.OFFICIAL_FILING:
            original = self._filings.get(filing_key)
            if original is not None:
                return _duplicate(
                    DeduplicationRule.REPEATED_FILING,
                    original,
                    "the same filing headline was already published by this source today",
                )
        original = self._by_headline.get(keys.headline)
        if original is not None:
            return _duplicate(
                DeduplicationRule.SYNDICATED_HEADLINE,
                original,
                "an identical headline was published elsewhere on the same day",
            )
        if keys.rewrite is not None:
            original = self._by_rewrite.get(keys.rewrite)
            if original is not None:
                return _duplicate(
                    DeduplicationRule.REWRITTEN_HEADLINE,
                    original,
                    "the same significant words were published on the same day",
                )
        return DeduplicationDecision(
            is_duplicate=False,
            rule=None,
            original=None,
            reason="no bounded rule matched an item already observed",
        )

    def _remember(self, item: NewsItem) -> None:
        """Index one new item under every key a later item may repeat."""
        keys = item.fingerprints
        self._by_identity[keys.identity] = item.identity
        self._by_url[keys.url] = item.identity
        self._by_headline.setdefault(keys.headline, item.identity)
        if item.source.tier is NewsSourceTier.OFFICIAL_FILING:
            self._filings.setdefault((item.identity.source_key, keys.headline), item.identity)
        if keys.rewrite is not None:
            self._by_rewrite.setdefault(keys.rewrite, item.identity)

    def extend(self, items: Iterable[NewsItem]) -> tuple[DeduplicationDecision, ...]:
        """Observe a batch in order and return one decision per item."""
        return tuple(self.observe(item) for item in items)

    def restore(
        self,
        stored: Iterable[tuple[NewsItemIdentity, NewsFingerprints]],
    ) -> None:
        """Seed the ledger from fingerprints already held in the archive.

        Deduplication has to reach across polls or every restart would re-admit
        yesterday's syndication. Rows produced under an older identity revision
        are skipped rather than compared: different normalisation makes their
        fingerprints answers to a different question.
        """
        for identity, keys in stored:
            if keys.revision != NEWS_IDENTITY_REVISION:
                continue
            self._by_identity.setdefault(keys.identity, identity)
            self._by_url.setdefault(keys.url, identity)
            self._by_headline.setdefault(keys.headline, identity)
            self._filings.setdefault((identity.source_key, keys.headline), identity)
            if keys.rewrite is not None:
                self._by_rewrite.setdefault(keys.rewrite, identity)


def _duplicate(
    rule: DeduplicationRule,
    original: NewsItemIdentity,
    reason: str,
) -> DeduplicationDecision:
    """Build a duplicate verdict that names its rule and its original."""
    return DeduplicationDecision(
        is_duplicate=True,
        rule=rule,
        original=original,
        reason=reason,
    )
