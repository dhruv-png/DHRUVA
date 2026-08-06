"""Provider-neutral news source health and fetch results.

An operational failure is never an empty success. A feed that timed out, a feed
that rate-limited us and a feed that genuinely had nothing new all produce zero
items, and collapsing them would make "no news today" indistinguishable from
"the news stopped arriving three weeks ago". Each gets its own health value, and
a result may carry items only when the poll actually succeeded.

Nothing here knows about HTTP, JSON or any particular provider. A transport maps
whatever it saw onto these values, and the ingestion path above reads only these.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import datetime

    from dhruva.contexts.intelligence.domain.news import NewsItem, NewsSource

__all__ = ["NewsFetchResult", "SourceHealth", "SourceStatus"]

_MAX_REASON = 240
_REVISION = re.compile(r"[a-z][a-z0-9_-]{1,63}\Z")
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MIN_HTTP_STATUS = 100
_MAX_HTTP_STATUS = 599


class SourceHealth(StrEnum):
    """Why one poll of one source ended the way it did."""

    #: The poll succeeded and returned at least one usable item.
    HEALTHY = "HEALTHY"
    #: The poll succeeded and the source genuinely had nothing to report.
    EMPTY_RESULT = "EMPTY_RESULT"
    #: Transport failure, timeout or a 5xx. Retry later; nothing is wrong here.
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    #: The source asked us to slow down. Distinct from unavailable because the
    #: correct response is to wait longer, not to retry harder.
    RATE_LIMITED = "RATE_LIMITED"
    #: Credentials or a session were rejected. Not reachable for an anonymous
    #: source, and present so one that later needs a key cannot silently
    #: degrade into "temporarily unavailable" forever.
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    #: Bytes arrived and could not be parsed at all.
    MALFORMED_PAYLOAD = "MALFORMED_PAYLOAD"
    #: The payload parsed, but its newest item is older than the caller's
    #: freshness requirement. The feed is up and has stopped moving.
    STALE = "STALE"
    #: The payload parsed into a shape this mapper does not claim to understand.
    #: Guessing at it is how a provider's silent format change becomes a month
    #: of quietly wrong data.
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """One poll's outcome, with enough detail to act on it."""

    health: SourceHealth
    reason: str
    observed_at: datetime
    http_status: int | None = None
    retry_after: timedelta | None = None

    def __post_init__(self) -> None:
        """Require a readable reason and a plausible transport detail."""
        invariant(bool(self.reason), "a source status must give a reason")
        invariant(len(self.reason) <= _MAX_REASON, "source status reason is too long")
        invariant(
            self.observed_at.tzinfo is not None and self.observed_at.utcoffset() is not None,
            "observed_at must be timezone-aware",
        )
        invariant(
            self.observed_at.utcoffset() == timedelta(0),
            "observed_at must be UTC",
        )
        if self.http_status is not None:
            invariant(
                _MIN_HTTP_STATUS <= self.http_status <= _MAX_HTTP_STATUS,
                "http status is outside the valid range",
            )
        if self.retry_after is not None:
            invariant(self.retry_after >= timedelta(0), "retry-after cannot be negative")

    @property
    def succeeded(self) -> bool:
        """Return whether the poll reached the source and understood its answer."""
        return self.health in {SourceHealth.HEALTHY, SourceHealth.EMPTY_RESULT}


@dataclass(frozen=True, slots=True)
class NewsFetchResult:
    """Everything one poll of one source produced, successful or not."""

    source: NewsSource
    status: SourceStatus
    items: tuple[NewsItem, ...]
    retrieved_at: datetime
    content_sha256: str
    mapper_revision: str

    def __post_init__(self) -> None:
        """Refuse a result whose items contradict its own health."""
        invariant(bool(_HEX_SHA256.fullmatch(self.content_sha256)), "invalid payload SHA-256")
        invariant(bool(_REVISION.fullmatch(self.mapper_revision)), "invalid mapper revision")
        invariant(
            self.retrieved_at.tzinfo is not None and self.retrieved_at.utcoffset() is not None,
            "retrieved_at must be timezone-aware",
        )
        if self.status.health is SourceHealth.HEALTHY:
            invariant(bool(self.items), "a healthy poll must carry the items it found")
        else:
            invariant(
                not self.items,
                "only a healthy poll carries items; every other outcome carries none",
            )
        invariant(
            all(item.source.key == self.source.key for item in self.items),
            "every item in a fetch result belongs to the source that produced it",
        )

    @property
    def usable_items(self) -> tuple[NewsItem, ...]:
        """Return the items an ingestion pass may act on."""
        return self.items if self.status.health is SourceHealth.HEALTHY else ()
