"""An in-memory :class:`EventPublisher` (ADR-067).

Exists for two reasons, and the second is the important one.

It lets the relay be built and tested before any transport exists. And it proves
the port is not Redis-shaped: a port with one implementation takes that
implementation's shape, and the shape is only discovered when a second adapter is
written against it -- by which time callers depend on the leak.

Deterministic by construction
-----------------------------
Failures are **programmed per event**, not sampled from a probability. A test
that says "fail this envelope twice, then accept it" asserts on an outcome; a
test that says "fail about 30% of the time" asserts on a coin. Nothing here
sleeps, and nothing depends on ordering between tasks, so a test can assert on
exact counts rather than on eventual consistency.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from uuid import UUID

from dhruva.shared.errors import UpstreamUnavailableError, ValidationError
from dhruva.shared.messaging import Delivery, EventEnvelope

if TYPE_CHECKING:
    from collections.abc import Sequence

    from dhruva.shared.messaging import AckToken

__all__ = ["InMemoryAck", "InMemoryBus", "InMemoryPublisher", "RecordedResult"]


@dataclass(frozen=True, slots=True)
class RecordedResult:
    """One batch's outcome, satisfying the :class:`PublishResult` protocol."""

    accepted: Sequence[EventEnvelope]
    rejected: Sequence[tuple[EventEnvelope, Exception]]


@dataclass(slots=True)
class InMemoryPublisher:
    """Records what it was asked to publish, and fails on request.

    Attributes
    ----------
    published
        Every envelope handed to :meth:`publish`, in order, **including
        duplicates**. Duplicates are the point: at-least-once means a redelivery
        is correct behaviour (ADR-062), and a recorder that de-duplicated would
        hide the very thing the relay's crash tests need to observe.
    transient_failures
        ``event_id`` to the number of times it should fail with a retryable
        error before succeeding. Decremented on each attempt, so
        ``{id: 2}`` means "fail, fail, then accept".
    terminal_failures
        ``event_id``s that always fail with a terminal error. These consume no
        retry budget (ADR-064) and go straight to the dead-letter state.
    unavailable
        When set, every envelope in every batch is rejected as retryable --
        the whole broker being down, rather than one message being bad.
    """

    published: list[EventEnvelope] = field(default_factory=list)
    batches: list[int] = field(default_factory=list)
    transient_failures: dict[UUID, int] = field(default_factory=dict)
    terminal_failures: set[UUID] = field(default_factory=set)
    unavailable: bool = False

    async def publish(self, envelopes: Sequence[EventEnvelope]) -> RecordedResult:
        """Accept or reject each envelope according to the programmed failures.

        Never raises for a programmed failure: the port requires an ordinary
        transport failure to be *reported* so the relay can schedule a retry,
        rather than raised so that one bad message costs the whole batch.
        """
        self.batches.append(len(envelopes))

        accepted: list[EventEnvelope] = []
        rejected: list[tuple[EventEnvelope, Exception]] = []

        for envelope in envelopes:
            error = self._failure_for(envelope.event_id)
            if error is None:
                self.published.append(envelope)
                accepted.append(envelope)
            else:
                rejected.append((envelope, error))

        return RecordedResult(accepted=accepted, rejected=rejected)

    def _failure_for(self, event_id: UUID) -> Exception | None:
        """Return the failure programmed for this event, consuming one transient."""
        if event_id in self.terminal_failures:
            return ValidationError("payload is not acceptable to this transport")
        if self.unavailable:
            return UpstreamUnavailableError("broker is unavailable")

        remaining = self.transient_failures.get(event_id, 0)
        if remaining > 0:
            self.transient_failures[event_id] = remaining - 1
            return UpstreamUnavailableError("transport refused this message")
        return None

    # ---------------------------------------------------------------- queries

    @property
    def published_ids(self) -> list[UUID]:
        """Event ids in publication order, duplicates included."""
        return [envelope.event_id for envelope in self.published]

    @property
    def unique_published_ids(self) -> set[UUID]:
        """Distinct event ids that reached the transport at least once."""
        return set(self.published_ids)

    def delivery_count(self, event_id: UUID) -> int:
        """How many times one event was published.

        Greater than one is **not** a failure. At-least-once permits it, and a
        crash between publishing and marking published produces it by design.
        """
        return Counter(self.published_ids)[event_id]

    def duplicated_ids(self) -> set[UUID]:
        """Event ids delivered more than once."""
        return {event_id for event_id, count in Counter(self.published_ids).items() if count > 1}


@dataclass(frozen=True, slots=True)
class InMemoryAck:
    """The opaque token acknowledging one in-memory delivery.

    An offset, and deliberately nothing a consumer could act on. The whole point
    of the conformance suite is that a consumer cannot tell which adapter it has
    (ADR-067), and a token carrying anything meaningful is where that starts to
    leak.
    """

    offset: int


@dataclass(slots=True)
class InMemoryBus:
    """A loopback satisfying both :class:`EventPublisher` and :class:`EventStream`.

    Exists so the ports have **two** implementations before the replay engine is
    written. A port with one implementation takes that implementation's shape,
    and the shape is only discovered when a second adapter is built against it --
    by which time callers depend on the leak.

    Serialises on publish and parses on read
    ----------------------------------------
    It would be simpler to hand back the object that was published, and it would
    make this class worthless as a peer: the conformance suite's round-trip
    assertion would hold vacuously, and a codec that lost a timezone would be
    caught by the Redis run alone. Envelopes cross this bus as canonical JSON,
    the same form they cross a real one (ADR-061).

    Not a place to program failures
    -------------------------------
    :class:`InMemoryPublisher` does that, and the relay's failure-injection suite
    uses it. This one is deliberately boring: it is the *reference* behaviour
    every transport must match, so behaviour it invents is behaviour Redis would
    then be judged against.
    """

    supported_versions: frozenset[int] | None = None
    documents: list[str] = field(default_factory=list)
    _delivered: int = 0
    _pending: set[int] = field(default_factory=set)

    async def publish(self, envelopes: Sequence[EventEnvelope]) -> RecordedResult:
        """Accept every envelope, recording its canonical form."""
        accepted = list(envelopes)
        self.documents.extend(envelope.to_json() for envelope in accepted)
        return RecordedResult(accepted=accepted, rejected=[])

    async def read(self, *, limit: int) -> Sequence[Delivery]:
        """Return up to ``limit`` undelivered envelopes, holding each until acknowledged."""
        if limit <= 0:
            raise ValidationError("limit must be positive", limit=limit)

        start = self._delivered
        end = min(start + limit, len(self.documents))
        self._delivered = end
        self._pending.update(range(start, end))

        return [
            Delivery(
                envelope=EventEnvelope.from_json(
                    self.documents[offset], supported_versions=self.supported_versions
                ),
                ack_token=InMemoryAck(offset=offset),
            )
            for offset in range(start, end)
        ]

    async def acknowledge(self, tokens: Sequence[AckToken]) -> int:
        """Confirm deliveries, returning how many were actually outstanding.

        A repeated acknowledgement counts zero rather than one. At-least-once
        means a consumer may acknowledge twice after a crash between its commit
        and its ack, and a count that could not tell the two apart would be a
        consumer-lag metric that quietly drifts.
        """
        acknowledged = 0
        for token in tokens:
            if not isinstance(token, InMemoryAck):
                raise ValidationError(
                    "acknowledge received a token this stream did not issue",
                    actual_type=type(token).__name__,
                )
            acknowledged += int(token.offset in self._pending)
            self._pending.discard(token.offset)
        return acknowledged

    async def pending(self) -> int:
        """Return how many deliveries are read but not yet acknowledged."""
        return len(self._pending)
