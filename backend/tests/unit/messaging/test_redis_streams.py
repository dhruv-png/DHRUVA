"""Translation between envelopes and Redis Streams (ADR-067, ADR-068).

What a fake can and cannot show
-------------------------------
These tests use a fake client, which is the right instrument for exactly one
kind of question: *given this reply, does the adapter produce the right thing?*
Reply shapes, error mapping, budget arithmetic and refusals are all decidable
without a broker, and a container would only make them slower.

It is the wrong instrument for consumer groups, acknowledgement, pending lists
and trimming, because a fake would be asserting that the code calls the methods
it calls. Those live in ``tests/integration/test_redis_streams.py`` against a
real Redis, and nothing here is a substitute for them.

The RESP2 and RESP3 reply shapes are transcribed from redis-py's own parsers
(``redis/_parsers/helpers.py``: ``parse_xread`` and ``parse_xread_resp3``),
because the protocol is a property of the injected client and this adapter does
not get to choose it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from redis import exceptions as redis_errors

from dhruva.contexts.platform.infrastructure.messaging.redis_streams import (
    ENVELOPE_FIELD,
    RedisAck,
    RedisEventStream,
    RedisStreamPublisher,
    stream_key,
)
from dhruva.shared.errors import (
    ConfigurationError,
    ExternalServiceError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
    ValidationError,
)
from dhruva.shared.messaging import (
    EventEnvelope,
    EventPublisher,
    EventStream,
    UnsupportedEventVersionError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

pytestmark = pytest.mark.unit

NOW = datetime(2026, 7, 30, 9, 15, tzinfo=UTC)
NAMESPACE = "test:events"


def envelope(
    *, sequence: int = 1, aggregate_type: str = "Order", version: int = 1
) -> EventEnvelope:
    """Build a minimal, valid envelope. The adapter is the subject, not the payload."""
    identifier = uuid4()
    return EventEnvelope(
        event_id=identifier,
        event_type="OrderFilled",
        event_version=version,
        aggregate_type=aggregate_type,
        aggregate_id=UUID(int=sequence),
        sequence=sequence,
        occurred_at=NOW,
        recorded_at=NOW,
        account_id=None,
        correlation_id=identifier,
        causation_id=None,
        payload={"quantity": 5},
    )


class FakeRedis:
    """Records commands and replays programmed replies.

    Deliberately not a Redis. It answers with the *shapes* redis-py produces so
    that the adapter's decoding is exercised, and it raises the exceptions
    redis-py raises so that the error mapping is exercised. It knows nothing
    about streams, and no test here asks it to.
    """

    def __init__(self) -> None:
        """Start with no programmed failures and no recorded calls."""
        self.added: list[tuple[str, dict[str, str], int | None, bool]] = []
        self.acked: list[tuple[str, str, tuple[str, ...]]] = []
        self.groups: list[tuple[str, str, str, bool]] = []
        self.read_calls: list[tuple[str, int]] = []
        self.pending_calls: list[tuple[str, str]] = []
        self.claim_calls: list[tuple[str, str, str, int]] = []
        self.replies: list[Any] = []
        self.raises: Exception | None = None
        self.pending_counts: dict[str, int] = {}

    async def xadd(
        self,
        name: str,
        fields: dict[str, str],
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> str:
        """Record the write, or raise what the test programmed."""
        self._maybe_raise()
        self.added.append((name, fields, maxlen, approximate))
        return f"{len(self.added)}-0"

    async def xgroup_create(self, name: str, groupname: str, **options: Any) -> bool:
        """Record the group creation, or raise what the test programmed.

        The optional arguments are taken as ``**options`` because one of them is
        named ``id``, which redis-py chose and this fake must match.
        """
        self._maybe_raise()
        self.groups.append((name, groupname, options.get("id", "$"), bool(options.get("mkstream"))))
        return True

    async def xreadgroup(
        self, groupname: str, consumername: str, streams: dict[str, str], **options: Any
    ) -> Any:
        """Return the next programmed reply, recording what was asked of which stream."""
        self._maybe_raise()
        (key,) = streams
        self.read_calls.append((key, int(options.get("count") or 0)))
        assert (groupname, consumername) != ("", ""), "the group identity must reach the client"
        return self.replies.pop(0) if self.replies else []

    async def xack(self, name: str, groupname: str, *ids: str) -> int:
        """Record the acknowledgement and report every id as newly acknowledged."""
        self._maybe_raise()
        self.acked.append((name, groupname, ids))
        return len(ids)

    async def xpending(self, name: str, groupname: str) -> dict[str, Any]:
        """Return the programmed pending summary for this stream."""
        self._maybe_raise()
        self.pending_calls.append((name, groupname))
        return {"pending": self.pending_counts.get(name, 0), "min": None, "max": None}

    async def xautoclaim(
        self, name: str, groupname: str, consumername: str, min_idle_time: int, **options: Any
    ) -> Any:
        """Record the claim attempt and return the next programmed reply."""
        self._maybe_raise()
        self.claim_calls.append((name, groupname, consumername, min_idle_time))
        assert options.get("start_id") == "0-0", "a claim must scan from the start of the list"
        return self.replies.pop(0) if self.replies else ["0-0", []]

    def _maybe_raise(self) -> None:
        """Raise the programmed transport failure, if there is one."""
        if self.raises is not None:
            raise self.raises


def resp2(key: str, *entries: tuple[str, dict[str, str]]) -> list[Any]:
    """Build an ``XREADGROUP`` reply in the RESP2 shape."""
    return [[key.encode(), [(eid.encode(), _encode(fields)) for eid, fields in entries]]]


def resp3(key: str, *entries: tuple[str, dict[str, str]]) -> dict[bytes, Any]:
    """Build an ``XREADGROUP`` reply in the RESP3 shape, including its extra nesting."""
    return {key.encode(): [[(eid.encode(), _encode(fields)) for eid, fields in entries]]}


def _encode(fields: dict[str, str]) -> dict[bytes, bytes]:
    """Encode one entry's field map the way a non-decoding client returns it."""
    return {name.encode(): value.encode() for name, value in fields.items()}


def entry(env: EventEnvelope, entry_id: str = "1-0") -> tuple[str, dict[str, str]]:
    """Build a well-formed stream entry carrying ``env``."""
    return (entry_id, {ENVELOPE_FIELD: env.to_json()})


def make_stream(
    client: FakeRedis,
    *,
    aggregate_types: Sequence[str] = ("Order",),
    supported_versions: frozenset[int] | None = None,
) -> RedisEventStream:
    """Build a stream bound to the fake, with the test namespace."""
    return RedisEventStream(
        client,  # type: ignore[arg-type]  # a fake standing in for redis.asyncio.Redis
        aggregate_types=aggregate_types,
        group="risk",
        consumer="worker-1",
        namespace=NAMESPACE,
        supported_versions=supported_versions,
    )


# --------------------------------------------------------------------------- #
# The adapters are the ports, structurally
# --------------------------------------------------------------------------- #


def test_the_adapters_satisfy_the_ports_they_claim() -> None:
    """A protocol that everything satisfies asserts nothing; these must actually fit."""
    client = FakeRedis()
    publisher = RedisStreamPublisher(client, namespace=NAMESPACE)  # type: ignore[arg-type]

    assert isinstance(publisher, EventPublisher)
    assert isinstance(make_stream(client), EventStream)
    assert not isinstance(object(), EventStream), "the protocol must discriminate"


def test_the_ack_token_is_frozen() -> None:
    """A mutable token could be edited between read and acknowledge."""
    token = RedisAck(stream="s", entry_id="1-0")
    with pytest.raises(AttributeError):
        token.entry_id = "2-0"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Key construction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("aggregate_type", ["Order", "Position", "Order_v2", "market.tick", "A-b"])
def test_an_ordinary_aggregate_type_becomes_a_namespaced_key(aggregate_type: str) -> None:
    """The routing value is the last path segment; the namespace is operational."""
    assert stream_key(aggregate_type, namespace="ns") == f"ns:{aggregate_type}"


@pytest.mark.parametrize(
    ("aggregate_type", "why"),
    [
        ("Order:Filled", "a colon would silently address a different stream"),
        ("Order*", "an asterisk would match a glob an operator later runs"),
        ("", "an empty type would collapse the key to the namespace"),
        ("1Order", "a leading digit is not a type name"),
        (" Order", "surrounding whitespace is invisible in a key listing"),
        ("Order\n", "a newline would break every line-oriented tool"),
        ("A" * 65, "an unbounded type would make an unbounded key"),
    ],
)
def test_an_unusable_aggregate_type_is_refused(aggregate_type: str, why: str) -> None:
    """Refused before Redis sees it, and refused as terminal so it dead-letters."""
    with pytest.raises(ValidationError):
        stream_key(aggregate_type)
    assert why


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_each_envelope_is_written_to_the_stream_for_its_aggregate_type() -> None:
    """One stream per aggregate type, and the envelope travels as canonical JSON."""
    client = FakeRedis()
    publisher = RedisStreamPublisher(client, namespace=NAMESPACE, maxlen=500)  # type: ignore[arg-type]
    order, position = envelope(), envelope(sequence=2, aggregate_type="Position")

    result = await publisher.publish([order, position])

    assert [key for key, _, _, _ in client.added] == [
        f"{NAMESPACE}:Order",
        f"{NAMESPACE}:Position",
    ]
    assert client.added[0][1] == {ENVELOPE_FIELD: order.to_json()}
    assert client.added[0][2] == 500, "the trim length must reach XADD"
    assert list(result.accepted) == [order, position]
    assert not result.rejected


@pytest.mark.asyncio
async def test_a_transport_failure_is_reported_rather_than_raised() -> None:
    """One bad message must not cost the batch; the relay needs to retry precisely."""
    client = FakeRedis()
    client.raises = redis_errors.ConnectionError("broker down")
    publisher = RedisStreamPublisher(client, namespace=NAMESPACE)  # type: ignore[arg-type]
    only = envelope()

    result = await publisher.publish([only])

    assert not result.accepted
    [(rejected, error)] = result.rejected
    assert rejected is only
    assert isinstance(error, UpstreamUnavailableError)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (redis_errors.TimeoutError("slow"), UpstreamTimeoutError),
        (redis_errors.ConnectionError("down"), UpstreamUnavailableError),
        (redis_errors.AuthenticationError("bad password"), ConfigurationError),
        (redis_errors.ResponseError("WRONGTYPE"), ExternalServiceError),
        (redis_errors.OutOfMemoryError("OOM"), ExternalServiceError),
    ],
)
async def test_redis_failures_map_onto_the_error_taxonomy(
    raised: Exception, expected: type[Exception]
) -> None:
    """Deliberate mapping, not inheritance.

    ``redis.ConnectionError`` does not inherit from the builtin, so without this
    every broker failure would reach :func:`classify` as an unrecognised error and
    be retried by a default rather than by a decision.
    """
    client = FakeRedis()
    client.raises = raised
    publisher = RedisStreamPublisher(client, namespace=NAMESPACE)  # type: ignore[arg-type]

    result = await publisher.publish([envelope()])

    [(_, error)] = result.rejected
    assert isinstance(error, expected)


@pytest.mark.asyncio
async def test_an_unusable_aggregate_type_is_rejected_without_reaching_redis() -> None:
    """A terminal failure (ADR-064): it dead-letters instead of spending twelve attempts."""
    client = FakeRedis()
    publisher = RedisStreamPublisher(client, namespace=NAMESPACE)  # type: ignore[arg-type]

    result = await publisher.publish([envelope(aggregate_type="Order:Filled")])

    assert not client.added, "nothing may be written for an unroutable envelope"
    [(_, error)] = result.rejected
    assert isinstance(error, ValidationError)


def test_a_publisher_refuses_a_trim_length_that_would_discard_every_write() -> None:
    """Zero maxlen trims the stream to nothing, which looks exactly like data loss."""
    with pytest.raises(ConfigurationError):
        RedisStreamPublisher(FakeRedis(), maxlen=0)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", [resp2, resp3], ids=["resp2", "resp3"])
async def test_an_entry_round_trips_back_into_the_envelope_that_was_published(
    shape: Any,
) -> None:
    """Both wire protocols decode identically; the client's choice is not the adapter's."""
    client = FakeRedis()
    sent = envelope(sequence=7)
    client.replies = [shape(f"{NAMESPACE}:Order", entry(sent, "1700-0"))]
    stream = make_stream(client)

    [delivery] = await stream.read(limit=10)

    assert delivery.envelope == sent
    assert delivery.ack_token == RedisAck(stream=f"{NAMESPACE}:Order", entry_id="1700-0")


@pytest.mark.asyncio
async def test_an_idle_stream_returns_nothing_rather_than_blocking() -> None:
    """A stream that blocked would take the polling decision from the consumer."""
    stream = make_stream(FakeRedis())

    assert await stream.read(limit=10) == []


@pytest.mark.asyncio
async def test_the_limit_is_a_budget_across_streams_not_a_count_per_stream() -> None:
    """``COUNT`` is per stream, so a single call could return N x limit entries.

    Anything over the limit would have been delivered into this consumer's pending
    list and then dropped on the floor -- read by nobody, acknowledged by nobody.
    """
    client = FakeRedis()
    client.replies = [
        resp2(f"{NAMESPACE}:Order", entry(envelope(sequence=1), "1-0")),
        resp2(f"{NAMESPACE}:Position", entry(envelope(sequence=2), "2-0")),
    ]
    stream = make_stream(client, aggregate_types=("Order", "Position"))

    deliveries = await stream.read(limit=2)

    assert len(deliveries) == 2
    assert [count for _, count in client.read_calls] == [2, 1], (
        "the second stream must be asked only for what the budget has left"
    )


@pytest.mark.asyncio
async def test_a_full_first_stream_does_not_starve_the_others() -> None:
    """The starting stream rotates, or a busy stream stalls every stream behind it.

    The first stream is deliberately *full*: it consumes the whole budget, so the
    second stream is never reached within a single call. Only rotation gets it
    read at all, and without rotation the assertion below sees ``Order`` twice --
    which is a consumer that never processes a Position for as long as orders
    keep arriving.
    """
    client = FakeRedis()
    busy = resp2(f"{NAMESPACE}:Order", entry(envelope(sequence=1), "1-0"))
    also_busy = resp2(
        f"{NAMESPACE}:Position", entry(envelope(sequence=2, aggregate_type="Position"), "2-0")
    )
    client.replies = [busy, also_busy]
    stream = make_stream(client, aggregate_types=("Order", "Position"))

    first = await stream.read(limit=1)
    second = await stream.read(limit=1)

    assert [key for key, _ in client.read_calls] == [
        f"{NAMESPACE}:Order",
        f"{NAMESPACE}:Position",
    ]
    assert [delivery.envelope.aggregate_type for delivery in (*first, *second)] == [
        "Order",
        "Position",
    ]


@pytest.mark.asyncio
async def test_a_non_positive_limit_is_a_caller_bug() -> None:
    """A zero-length read would silently do nothing and look like an idle bus."""
    stream = make_stream(FakeRedis())
    with pytest.raises(ValidationError):
        await stream.read(limit=0)


@pytest.mark.asyncio
async def test_a_malformed_entry_raises_rather_than_being_skipped() -> None:
    """ADR-022: ambiguity blocks rather than proceeds.

    Skipping would process the events around the hole and leave nothing that
    shows there was one. Raising leaves the entry in the pending list, where it
    is recoverable and visible.
    """
    client = FakeRedis()
    client.replies = [resp2(f"{NAMESPACE}:Order", ("1-0", {ENVELOPE_FIELD: "{not json"}))]
    stream = make_stream(client)

    with pytest.raises(ValidationError):
        await stream.read(limit=10)


@pytest.mark.asyncio
async def test_an_entry_without_an_envelope_field_raises() -> None:
    """Something else wrote to this stream, and guessing at its meaning is worse."""
    client = FakeRedis()
    client.replies = [resp2(f"{NAMESPACE}:Order", ("1-0", {"payload": "{}"}))]
    stream = make_stream(client)

    with pytest.raises(ValidationError):
        await stream.read(limit=10)


@pytest.mark.asyncio
async def test_an_unsupported_schema_version_raises_its_own_error_type() -> None:
    """ADR-061. The caller must be able to tell a version gap from a corrupt payload."""
    client = FakeRedis()
    client.replies = [resp2(f"{NAMESPACE}:Order", entry(envelope(version=3), "1-0"))]
    stream = make_stream(client, supported_versions=frozenset({1, 2}))

    with pytest.raises(UnsupportedEventVersionError):
        await stream.read(limit=10)


@pytest.mark.asyncio
async def test_a_reply_element_that_is_neither_bytes_nor_text_is_refused() -> None:
    """Coercing an unexpected reply with ``str()`` would invent a value."""
    client = FakeRedis()
    client.replies = [[[f"{NAMESPACE}:Order".encode(), [(b"1-0", {b"envelope": 42})]]]]
    stream = make_stream(client)

    with pytest.raises(ExternalServiceError):
        await stream.read(limit=10)


@pytest.mark.asyncio
async def test_an_unrecognised_reply_shape_is_refused_rather_than_read_as_idle() -> None:
    """Returning nothing for a reply nobody understands looks exactly like an idle bus."""
    client = FakeRedis()
    client.replies = ["this is not a reply"]
    stream = make_stream(client)

    with pytest.raises(ExternalServiceError):
        await stream.read(limit=10)


@pytest.mark.asyncio
async def test_a_trimmed_entry_is_not_delivered_as_an_empty_one() -> None:
    """Redis reports an entry trimmed away under a consumer's feet as a pair of nulls."""
    client = FakeRedis()
    client.replies = [[[f"{NAMESPACE}:Order".encode(), [(None, None)]]]]
    stream = make_stream(client)

    assert await stream.read(limit=10) == []


# --------------------------------------------------------------------------- #
# Acknowledgement
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_acknowledgement_is_grouped_by_stream() -> None:
    """One XACK per stream, not one per entry."""
    client = FakeRedis()
    stream = make_stream(client, aggregate_types=("Order", "Position"))

    acknowledged = await stream.acknowledge(
        [
            RedisAck(stream=f"{NAMESPACE}:Order", entry_id="1-0"),
            RedisAck(stream=f"{NAMESPACE}:Order", entry_id="2-0"),
            RedisAck(stream=f"{NAMESPACE}:Position", entry_id="3-0"),
        ]
    )

    assert acknowledged == 3
    assert len(client.acked) == 2
    assert client.acked[0][2] == ("1-0", "2-0")


@pytest.mark.asyncio
async def test_a_token_this_stream_did_not_issue_is_refused() -> None:
    """Ignoring it would leave the entry pending forever and report a smaller count."""
    stream = make_stream(FakeRedis())

    with pytest.raises(ValidationError):
        await stream.acknowledge(["1-0"])


@pytest.mark.asyncio
async def test_a_token_for_an_unsubscribed_stream_is_refused() -> None:
    """A token from another consumer's stream is a wiring mistake, not a no-op."""
    stream = make_stream(FakeRedis())

    with pytest.raises(ValidationError):
        await stream.acknowledge([RedisAck(stream="other:Order", entry_id="1-0")])


@pytest.mark.asyncio
async def test_acknowledging_nothing_touches_nothing() -> None:
    """An empty commit is not an error, and must not issue a command."""
    client = FakeRedis()

    assert await make_stream(client).acknowledge([]) == 0
    assert not client.acked


# --------------------------------------------------------------------------- #
# Group creation, lag and reclaim
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_group_is_created_from_the_beginning_of_the_stream() -> None:
    """The group starts at zero, and creates the stream if the producer has not.

    Starting at ``$`` would skip everything already buffered, which is
    indistinguishable from losing it. Redelivery is absorbed by the ledger
    (ADR-065), so given the choice, redeliver.
    """
    client = FakeRedis()
    await make_stream(client, aggregate_types=("Order", "Position")).ensure_group()

    assert [(name, start, mkstream) for name, _, start, mkstream in client.groups] == [
        (f"{NAMESPACE}:Order", "0", True),
        (f"{NAMESPACE}:Position", "0", True),
    ]


@pytest.mark.asyncio
async def test_creating_a_group_that_already_exists_is_not_a_failure() -> None:
    """``ensure_group`` is called on every start; the second start must be quiet."""
    client = FakeRedis()
    client.raises = redis_errors.ResponseError("BUSYGROUP Consumer Group name already exists")

    await make_stream(client).ensure_group()


@pytest.mark.asyncio
async def test_a_real_failure_creating_a_group_is_not_swallowed() -> None:
    """Only BUSYGROUP is benign; everything else must surface."""
    client = FakeRedis()
    client.raises = redis_errors.ResponseError("WRONGTYPE Operation against a key")

    with pytest.raises(ExternalServiceError):
        await make_stream(client).ensure_group()


@pytest.mark.asyncio
async def test_pending_sums_across_every_subscribed_stream() -> None:
    """Consumer lag is a property of the consumer, not of one of its streams."""
    client = FakeRedis()
    client.pending_counts = {f"{NAMESPACE}:Order": 4, f"{NAMESPACE}:Position": 3}
    stream = make_stream(client, aggregate_types=("Order", "Position"))

    assert await stream.pending() == 7


@pytest.mark.asyncio
async def test_asking_for_lag_before_the_group_exists_says_so() -> None:
    """The fix is a missing ``ensure_group`` call, and the message must name it."""
    client = FakeRedis()
    client.raises = redis_errors.ResponseError("NOGROUP No such consumer group")

    with pytest.raises(ConfigurationError):
        await make_stream(client).pending()


@pytest.mark.asyncio
async def test_reclaim_refuses_a_zero_idle_threshold() -> None:
    """Zero would take entries away from a consumer that is still working on them."""
    stream = make_stream(FakeRedis())

    with pytest.raises(ValidationError):
        await stream.reclaim(min_idle=timedelta(0))


@pytest.mark.asyncio
async def test_reclaim_reads_both_the_six_two_and_the_seven_reply_shapes() -> None:
    """Redis 7 added a third element. Read by position, so both versions work."""
    sent = envelope(sequence=9)
    for reply in (["0-0", [entry_bytes(sent)]], ["0-0", [entry_bytes(sent)], []]):
        client = FakeRedis()
        client.replies = [reply]

        [delivery] = await make_stream(client).reclaim(min_idle=timedelta(seconds=30))

        assert delivery.envelope == sent


def entry_bytes(env: EventEnvelope) -> tuple[bytes, dict[bytes, bytes]]:
    """Build a claimed entry the way redis-py hands one back."""
    return (b"5-0", {ENVELOPE_FIELD.encode(): env.to_json().encode()})


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"aggregate_types": []}, "a stream with nothing to read always looks idle"),
        ({"group": ""}, "an unnamed group cannot be shared between processes"),
        ({"consumer": ""}, "an unnamed consumer shares a pending list with every other"),
    ],
)
def test_a_misconfigured_stream_refuses_to_be_built(kwargs: dict[str, Any], why: str) -> None:
    """Each of these fails silently at runtime if it is allowed through here."""
    settings: dict[str, Any] = {
        "aggregate_types": ["Order"],
        "group": "risk",
        "consumer": "worker-1",
        "namespace": NAMESPACE,
    }
    settings.update(kwargs)

    with pytest.raises(ConfigurationError):
        RedisEventStream(FakeRedis(), **settings)  # type: ignore[arg-type]
    assert why
