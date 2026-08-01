"""One suite every transport must pass, unchanged (ADR-067).

Not a test module. A base class, subclassed once per adapter, so that the
assertions below are written exactly once and run against every implementation
of :class:`EventPublisher` and :class:`EventStream`.

Why this exists
---------------
ADR-067 says live delivery and replay are **peers**, and ADR-010 says one body of
strategy code serves backtest, paper and live. Both promises reduce to a single
testable claim: *a consumer cannot tell which adapter it has*. Nothing enforces
that claim except a suite that runs against all of them, and without one, two
adapters drift quietly -- the drift surfacing as a backtest that does not match
production, discovered after the strategy is trusted with money.

So the rule is that a new adapter subclasses this and passes it before anything
is wired to it. If a statement here is false for some future transport, that is
a finding about the transport or about the port, and it is settled by amending
the port rather than by exempting the adapter.

What may and may not be asserted here
-------------------------------------
Only the port's own vocabulary. No stream keys, no consumer groups, no offsets,
no entry ids, no container fixtures. The moment a conformance test needs one of
those it has stopped testing the port and started testing an adapter, and it
belongs in that adapter's own module.

The same boundary excludes *delivery* guarantees. At-least-once, and therefore
redelivery, is what ADR-062 promises of a live transport -- it is the cost of a
relay that may crash between publishing and recording that it published.
A replay has no such window: it reads a set of durable facts, each recorded
once, and the outbox's ``UNIQUE(event_id)`` is what makes that true. A suite
asserting redelivery would therefore be asserting a property of one transport
and calling it a property of the port.

That distinction was found by running this suite against replay, which failed
exactly one assertion and failed it for a reason that was correct. The
redelivery test now lives in ``tests/integration/test_redis_streams.py``, beside
the transport that actually promises it. Nothing was weakened: the shared suite
asserts less, and what it asserts is now true of every adapter rather than of
most of them.

Setup that *is* adapter-specific -- creating a consumer group, choosing a key
namespace -- happens in the ``bus`` fixture each subclass provides, which hands
back a pair that is already ready to use.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from dhruva.shared.errors import ValidationError
from dhruva.shared.messaging import (
    Delivery,
    EventEnvelope,
    EventPublisher,
    EventStream,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["Bus", "TransportConformance", "conformance_envelope"]

NOW = datetime(2026, 7, 31, 9, 15, tzinfo=UTC)

#: One aggregate type throughout. Ordering across aggregate types is explicitly
#: *not* guaranteed (ADR-062), so a conformance suite that mixed them would be
#: asserting something no transport promises.
AGGREGATE_TYPE = "Order"


def conformance_envelope(sequence: int, *, version: int = 1) -> EventEnvelope:
    """Build a valid envelope carrying values that survive nothing by accident.

    The payload deliberately includes a value that a lax codec would mangle: an
    integer large enough to lose precision if anything on the path routed it
    through a float (ADR-061 refuses floats outright, and this is how a
    regression would show).
    """
    identifier = uuid4()
    return EventEnvelope(
        event_id=identifier,
        event_type="OrderFilled",
        event_version=version,
        aggregate_type=AGGREGATE_TYPE,
        aggregate_id=UUID(int=sequence),
        sequence=sequence,
        occurred_at=NOW,
        recorded_at=NOW,
        account_id=None,
        correlation_id=identifier,
        causation_id=None,
        payload={"quantity": 9_007_199_254_740_993, "symbol": "NIFTY"},
    )


@dataclass(frozen=True, slots=True)
class Bus:
    """A publisher and a stream, already wired to each other and ready to use."""

    publisher: EventPublisher
    stream: EventStream


class TransportConformance:
    """Subclass this and supply a ``bus`` fixture. Add nothing; override nothing.

    A subclass that overrides a test here is a subclass declaring its transport
    is not a peer, which is the thing ADR-067 exists to prevent. If one of these
    genuinely cannot hold, raise it as an architectural finding rather than
    silently narrowing the contract.
    """

    # ----------------------------------------------------------------- shape

    async def test_the_adapters_satisfy_the_ports(self, bus: Bus) -> None:
        """Structural conformance, before any behavioural claim rests on it."""
        assert isinstance(bus.publisher, EventPublisher)
        assert isinstance(bus.stream, EventStream)

    # ------------------------------------------------------------ round trip

    async def test_an_envelope_arrives_exactly_as_it_was_published(self, bus: Bus) -> None:
        """The canonical form is what crosses, unchanged (ADR-061).

        Compared as JSON rather than as objects, because object equality would
        also hold if a codec had widened an integer or dropped a timezone on both
        sides symmetrically.
        """
        sent = conformance_envelope(1)

        await bus.publisher.publish([sent])
        [delivery] = await bus.stream.read(limit=10)

        assert delivery.envelope.to_json() == sent.to_json()
        assert delivery.envelope.payload["quantity"] == sent.payload["quantity"]

    async def test_a_batch_is_reported_envelope_by_envelope(self, bus: Bus) -> None:
        """The relay needs to know *which* envelopes landed, not merely how many."""
        sent = [conformance_envelope(n) for n in range(1, 4)]

        result = await bus.publisher.publish(sent)

        assert [envelope.event_id for envelope in result.accepted] == [e.event_id for e in sent]
        assert not result.rejected

    async def test_publication_order_is_delivery_order(self, bus: Bus) -> None:
        """Per-aggregate-type ordering, which is the whole of what ADR-062 promises.

        A replay whose order differed from live would make a backtest an
        approximation of production rather than a re-execution of it.
        """
        sent = [conformance_envelope(n) for n in range(1, 6)]
        await bus.publisher.publish(sent)

        deliveries = await bus.stream.read(limit=10)

        assert [d.envelope.sequence for d in deliveries] == [1, 2, 3, 4, 5]

    # --------------------------------------------------------------- reading

    async def test_an_idle_stream_returns_nothing_rather_than_blocking(self, bus: Bus) -> None:
        """A stream that blocked would take the polling cadence from the consumer.

        It would also make a replay's duration a function of the transport rather
        than of the data, which ADR-069 does not permit.
        """
        assert await bus.stream.read(limit=10) == []

    async def test_read_never_returns_more_than_the_limit(self, bus: Bus) -> None:
        """The limit is a promise about memory and about how much a crash strands."""
        await bus.publisher.publish([conformance_envelope(n) for n in range(1, 8)])

        first = await bus.stream.read(limit=3)
        second = await bus.stream.read(limit=3)

        assert len(first) == 3
        assert len(second) == 3
        assert {d.envelope.event_id for d in first}.isdisjoint(d.envelope.event_id for d in second)

    async def test_a_non_positive_limit_is_refused(self, bus: Bus) -> None:
        """A zero-length read would do nothing and look exactly like an idle bus."""
        with pytest.raises(ValidationError):
            await bus.stream.read(limit=0)

    async def test_an_unacknowledged_delivery_is_not_offered_again(self, bus: Bus) -> None:
        """Reading is not delivery. The entry is held until the consumer commits.

        The window between reading and acknowledging is exactly where ADR-065's
        ledger write goes; a transport that re-offered inside that window would
        make the ledger race against itself.
        """
        await bus.publisher.publish([conformance_envelope(1)])

        assert len(await bus.stream.read(limit=10)) == 1
        assert await bus.stream.read(limit=10) == []

    # ------------------------------------------------------- acknowledgement

    async def test_reading_makes_a_delivery_pending_and_acknowledging_clears_it(
        self, bus: Bus
    ) -> None:
        """``pending`` is the consumer-lag signal ADR-035 requires of every transport."""
        await bus.publisher.publish([conformance_envelope(1), conformance_envelope(2)])

        deliveries = await bus.stream.read(limit=10)
        assert await bus.stream.pending() == 2

        assert await bus.stream.acknowledge([d.ack_token for d in deliveries]) == 2
        assert await bus.stream.pending() == 0

    async def test_acknowledging_twice_reports_nothing_the_second_time(self, bus: Bus) -> None:
        """A consumer that crashed between committing and acking will ack twice.

        The count must reflect what actually changed, or a caller cannot tell a
        redelivery from a defect.
        """
        await bus.publisher.publish([conformance_envelope(1)])
        [delivery] = await bus.stream.read(limit=10)

        assert await bus.stream.acknowledge([delivery.ack_token]) == 1
        assert await bus.stream.acknowledge([delivery.ack_token]) == 0

    async def test_acknowledging_nothing_is_not_an_error(self, bus: Bus) -> None:
        """A transaction that produced no side effects still commits."""
        assert await bus.stream.acknowledge([]) == 0

    async def test_a_token_this_stream_did_not_issue_is_refused(self, bus: Bus) -> None:
        """Ignoring it would leave the delivery pending forever and report a smaller count."""
        with pytest.raises(ValidationError):
            await bus.stream.acknowledge(["not-a-token-any-adapter-issued"])

    # ------------------------------------------------------------- opacity

    async def test_the_ack_token_carries_nothing_a_consumer_can_act_on(self, bus: Bus) -> None:
        """ADR-067: a consumer that can inspect a token can branch on it.

        A consumer that branches on a transport detail is a consumer that cannot
        run against replay, which is the promise this whole port exists to keep.
        The token is asserted to be *hashable and comparable* -- everything a
        consumer legitimately needs -- and nothing else is asserted about it,
        because nothing else may be relied upon.
        """
        await bus.publisher.publish([conformance_envelope(1)])
        [delivery] = await bus.stream.read(limit=10)

        assert isinstance(delivery, Delivery)
        assert hash(delivery.ack_token) is not None
        assert delivery.ack_token == delivery.ack_token

    async def test_deliveries_are_frozen(self, bus: Bus) -> None:
        """A delivery a consumer can edit is a delivery two consumers disagree about."""
        await bus.publisher.publish([conformance_envelope(1)])
        [delivery] = await bus.stream.read(limit=10)

        with pytest.raises(AttributeError):
            delivery.ack_token = object()  # type: ignore[misc]


def sequences(deliveries: Sequence[Delivery]) -> list[int]:
    """Return the sequence numbers of a batch, for readable assertions."""
    return [delivery.envelope.sequence for delivery in deliveries]
