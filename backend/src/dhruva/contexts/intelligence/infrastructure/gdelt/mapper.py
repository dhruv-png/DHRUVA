"""Deterministic mapping of GDELT DOC 2.0 ``artlist`` JSON onto news items.

Pure: bytes in, a :class:`NewsFetchResult` out. No sockets, no clock, no
randomness. The transport hands this whatever it received and this decides what,
if anything, it means -- which is what lets every failure mode be tested without
a network.

**Documented fields only.** The GDELT DOC 2.0 API is documented at
https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/ and this mapper reads
``url``, ``title``, ``seendate``, ``domain``, ``language`` and ``sourcecountry``.
An unrecognised top-level shape is ``UNSUPPORTED_SCHEMA`` rather than a guess: a
provider's silent format change should stop ingestion, not quietly reinterpret it.

**Attribution.** GDELT's terms (https://www.gdeltproject.org/about.html) permit
unrestricted use without fee and require "a citation to the GDELT Project and a
link to this website". Every mapped item therefore carries GDELT as its source,
GDELT's homepage as the attribution link, and the originating publisher's domain
in the source display name, alongside the publisher's own article URL.

**Two honesty notes carried in the data rather than assumed away.**

``seendate`` is when *GDELT* first saw the article, not when the publisher
stamped it. It is the only date the documented response carries, so it becomes
``published_at`` -- an upper bound on true publication. ``first_seen_at`` is
DHRUVA's own retrieval instant, always at or after it.

GDELT supplies no stable per-item identifier. The provider item id is derived
from the canonical URL and prefixed to say so, because inventing a field and
inventing a *derivation* are different acts and only the second is defensible.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from dhruva.contexts.intelligence.domain.news import (
    MAX_TITLE,
    NewsItem,
    NewsItemIdentity,
    NewsSource,
    NewsSourceTier,
    PermittedText,
    canonical_url,
)
from dhruva.contexts.intelligence.domain.sources import (
    NewsFetchResult,
    SourceHealth,
    SourceStatus,
)
from dhruva.shared.errors import ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "GDELT_ATTRIBUTION_URL",
    "GDELT_MAPPER_REVISION",
    "GDELT_SOURCE_KEY",
    "map_artlist",
]

#: Mapper identity. Changing which fields are read, or how, changes this.
GDELT_MAPPER_REVISION = "gdelt-doc2-artlist-v1"

#: Stable source key for everything this mapper produces.
GDELT_SOURCE_KEY = "gdelt"

#: The citation link GDELT's terms require to accompany any use of the data.
GDELT_ATTRIBUTION_URL = "https://gdeltproject.org"

#: Longest payload this mapper will parse. A feed that suddenly returns tens of
#: megabytes is a defect somewhere, and reading it to find out is not free.
MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_ARTICLES = 250

_SEEN_DATE = re.compile(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z\Z")
_DOMAIN = re.compile(r"[A-Za-z0-9.-]{1,120}\Z")
_MAX_DISPLAY_NAME = 120


def _status(
    health: SourceHealth,
    reason: str,
    observed_at: datetime,
) -> SourceStatus:
    """Build a status for an outcome the mapper decided by itself."""
    return SourceStatus(health=health, reason=reason, observed_at=observed_at)


def _result(
    payload: bytes,
    *,
    status: SourceStatus,
    items: tuple[NewsItem, ...],
    retrieved_at: datetime,
) -> NewsFetchResult:
    """Wrap one outcome with the digest of exactly the bytes that produced it."""
    return NewsFetchResult(
        source=gdelt_source(),
        status=status,
        items=items,
        retrieved_at=retrieved_at,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        mapper_revision=GDELT_MAPPER_REVISION,
    )


def gdelt_source(publisher_domain: str | None = None) -> NewsSource:
    """Return the GDELT source, naming the originating publisher when known.

    The key stays ``gdelt`` for every item, because that is who DHRUVA fetched
    from and it is what deduplication and point-in-time reads are keyed by. The
    publisher appears in the display name so attribution survives into the
    archive without a schema change and without pretending GDELT wrote the piece.
    """
    display = "GDELT Project"
    if publisher_domain is not None:
        candidate = f"GDELT Project (via {publisher_domain})"
        if len(candidate) <= _MAX_DISPLAY_NAME:
            display = candidate
    return NewsSource(
        key=GDELT_SOURCE_KEY,
        display_name=display,
        tier=NewsSourceTier.AGGREGATOR,
        homepage_url=GDELT_ATTRIBUTION_URL,
    )


def _parse_seen_date(value: str) -> datetime:
    """Convert GDELT's ``YYYYMMDDTHHMMSSZ`` stamp into an aware UTC instant."""
    match = _SEEN_DATE.fullmatch(value.strip())
    if match is None:
        raise ValidationError("GDELT seendate is not in the documented format")
    year, month, day, hour, minute, second = (int(part) for part in match.groups())
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _text(row: dict[str, Any], field: str) -> str | None:
    """Return one string field, or ``None`` when it is absent or not text."""
    value = row.get(field)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _article(
    row: dict[str, Any],
    *,
    retrieved_at: datetime,
) -> NewsItem | None:
    """Map one documented article row, or decline it.

    Declining is per-row and silent by design: one malformed entry in a feed of
    two hundred is a bad row, not a bad feed, and failing the whole poll for it
    would trade a little data for all of it. A payload whose *shape* is wrong is
    a different matter and is refused above.
    """
    url = _text(row, "url")
    title = _text(row, "title")
    seen = _text(row, "seendate")
    if url is None or title is None or seen is None:
        return None
    try:
        link = canonical_url(url)
        published_at = _parse_seen_date(seen)
    except ValidationError:
        return None
    if len(title) > MAX_TITLE:
        title = title[:MAX_TITLE].rstrip()
    if not title:
        return None

    domain = _text(row, "domain")
    if domain is not None and not _DOMAIN.fullmatch(domain):
        domain = None
    # An article GDELT saw after our retrieval instant would break the
    # observed-after-publication rule. Clamping would invent a timestamp, so the
    # row is declined and the clock disagreement stays visible.
    if published_at > retrieved_at:
        return None
    return NewsItem(
        identity=NewsItemIdentity(
            source_key=GDELT_SOURCE_KEY,
            provider_item_id=_derived_item_id(link),
            url=link,
        ),
        source=gdelt_source(domain),
        text=PermittedText(title=title),
        published_at=published_at,
        first_seen_at=retrieved_at,
    )


def _derived_item_id(link: str) -> str:
    """Derive a stable item identity from the canonical URL, and say so."""
    return f"url-sha256:{hashlib.sha256(link.encode()).hexdigest()}"


def _documented_rows(
    payload: bytes,
) -> tuple[Sequence[object] | None, tuple[SourceHealth, str] | None]:
    """Return the documented ``articles`` rows, or why the payload is unusable.

    One place decides whether a response is even the shape GDELT documents, so
    the mapper below reads as the three answers it can give rather than as seven
    early exits.
    """
    if len(payload) > MAX_PAYLOAD_BYTES:
        return None, (
            SourceHealth.UNSUPPORTED_SCHEMA,
            "GDELT payload exceeded the safe response bound",
        )
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, (SourceHealth.MALFORMED_PAYLOAD, "GDELT response was not valid JSON")
    if not isinstance(document, dict) or not isinstance(document.get("articles"), list):
        return None, (
            SourceHealth.UNSUPPORTED_SCHEMA,
            "GDELT response has no documented 'articles' list",
        )
    articles: list[object] = document["articles"]
    if len(articles) > MAX_ARTICLES:
        return None, (
            SourceHealth.UNSUPPORTED_SCHEMA,
            "GDELT returned more articles than the documented maximum",
        )
    return articles, None


def map_artlist(
    payload: bytes,
    *,
    retrieved_at: datetime,
    fresh_within: timedelta | None = None,
) -> NewsFetchResult:
    """Map one GDELT ``mode=artlist&format=json`` response into a fetch result.

    ``fresh_within``, when given, is the age beyond which a parsed-but-stagnant
    feed is reported ``STALE`` rather than healthy. A feed that is up and has
    stopped moving is an operational problem, and it looks exactly like a quiet
    news day unless something says otherwise.
    """
    rows, rejection = _documented_rows(payload)
    if rejection is not None:
        return _result(
            payload,
            status=_status(rejection[0], rejection[1], retrieved_at),
            items=(),
            retrieved_at=retrieved_at,
        )
    assert rows is not None  # noqa: S101 - narrowed by the rejection above

    mapped: list[NewsItem] = []
    seen_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = _article(row, retrieved_at=retrieved_at)
        # One URL twice in one response is the provider repeating itself, and
        # staging it twice would fail the archive's own batch check.
        if item is not None and item.identity.provider_item_id not in seen_ids:
            seen_ids.add(item.identity.provider_item_id)
            mapped.append(item)

    if not mapped:
        return _result(
            payload,
            status=_status(
                SourceHealth.EMPTY_RESULT,
                "GDELT returned no usable articles for this query",
                retrieved_at,
            ),
            items=(),
            retrieved_at=retrieved_at,
        )

    newest = max(item.published_at for item in mapped)
    if fresh_within is not None and retrieved_at - newest > fresh_within:
        return _result(
            payload,
            status=SourceStatus(
                health=SourceHealth.STALE,
                reason=f"newest GDELT article is older than {fresh_within}",
                observed_at=retrieved_at,
            ),
            items=(),
            retrieved_at=retrieved_at,
        )
    return _result(
        payload,
        status=_status(
            SourceHealth.HEALTHY,
            f"GDELT returned {len(mapped)} usable articles",
            retrieved_at,
        ),
        items=tuple(sorted(mapped, key=lambda item: (item.published_at, item.identity.url))),
        retrieved_at=retrieved_at,
    )
