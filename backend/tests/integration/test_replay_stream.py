"""Replay over persisted events (ADR-062, ADR-067, ADR-069).

The claim that matters is the ``as_of`` bound, and it is asserted as a
*structural* property rather than a filter: an event recorded after the bound is
unreachable through the object, not merely absent from one query.

Real PostgreSQL, because every one of these statements is about what a query
returns. A fake would be asserting that the code calls the methods it calls.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from dhruva.contexts.platform.infrastructure.messaging import OutboxReplayStream, ReplayAck
from dhruva.shared.errors import ValidationError
from dhruva.shared.messaging import UnsupportedEventVersionError, encode_value

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

OCCURRED = datetime(2026, 7, 31, 9, 15, tzinfo=UTC)
#: The instant a replay pretends to be. Events recorded after it are the future.
AS_OF = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)

_INSERT = text(
    "INSERT INTO outbox (event_id, event_type, event_version, aggregate_type, "
    " aggregate_id, correlation_id, payload, occurred_at, recorded_at, attempts) "
    "VALUES (:event_id, 'OrderFilled', :version, 'Order', :aggregate_id, "
    " :event_id, :payload, :occurred_at, :recorded_at, 0)"
)


async def record(
    engine: AsyncEngine,
    *,
    recorded_at: datetime,
    occurred_at: datetime = OCCURRED,
    version: int = 1,
    quantity: int = 5,
) -> UUID:
    """Write one outbox row, returning its event id."""
    event_id = uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            _INSERT,
            {
                "event_id": event_id,
                "version": version,
                "aggregate_id": uuid4(),
                "payload": json.dumps({"quantity": encode_value(quantity)}),
                "occurred_at": occurred_at,
                "recorded_at": recorded_at,
            },
        )
    return event_id


async def emptied(engine: AsyncEngine) -> None:
    """Empty the outbox and reset its ordinal, so sequences start at one."""
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE outbox RESTART IDENTITY CASCADE"))


def replay(engine: AsyncEngine, **kwargs: object) -> OutboxReplayStream:
    """Build a replay bound to :data:`AS_OF` unless a test says otherwise."""
    return OutboxReplayStream(engine, as_of=AS_OF, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The as_of bound
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_an_event_recorded_after_the_bound_is_unreachable(
    migrated: AsyncEngine,
) -> None:
    """ADR-069, and the reason it is a constructor parameter rather than a filter.

    A replay that returned this row would hand a strategy a correction that
    arrived after the moment being replayed. The backtest would then trade on
    knowledge it did not have -- and would look *better* for it, which is the
    direction that gets capital committed.
    """
    await emptied(migrated)
    inside = await record(migrated, recorded_at=AS_OF - timedelta(hours=1))
    await record(migrated, recorded_at=AS_OF + timedelta(seconds=1))

    stream = replay(migrated)

    assert [d.envelope.event_id for d in await stream.read(limit=10)] == [inside]
    assert await stream.read(limit=10) == [], "the later row is not merely deferred"


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_bound_is_inclusive_of_its_own_instant(migrated: AsyncEngine) -> None:
    """An event recorded exactly at the bound was known at that moment."""
    await emptied(migrated)
    exactly = await record(migrated, recorded_at=AS_OF)

    assert [d.envelope.event_id for d in await replay(migrated).read(limit=10)] == [exactly]


@pytest.mark.usefixtures("truncated_after_test")
async def test_occurred_at_does_not_widen_the_bound(migrated: AsyncEngine) -> None:
    """The bound is on ``recorded_at``, which is the whole of ADR-007's point.

    A late-arriving correction *occurred* before the bound and was *learned*
    after it. Selecting on ``occurred_at`` would admit it, which is precisely the
    look-ahead this design exists to make impossible.
    """
    await emptied(migrated)
    await record(
        migrated,
        occurred_at=AS_OF - timedelta(days=2),
        recorded_at=AS_OF + timedelta(days=1),
    )

    assert await replay(migrated).read(limit=10) == []


async def test_a_naive_bound_is_refused(migrated: AsyncEngine) -> None:
    """A bound with no timezone silently admits hours of the future (ADR-006)."""
    with pytest.raises(ValidationError):
        OutboxReplayStream(migrated, as_of=datetime(2026, 7, 31, 12, 0))  # noqa: DTZ001


# --------------------------------------------------------------------------- #
# Order and progress
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_events_arrive_in_total_sequence_order(migrated: AsyncEngine) -> None:
    """ADR-062 promises live delivery per-aggregate order and replay a *total* one."""
    await emptied(migrated)
    for index in range(5):
        await record(migrated, recorded_at=AS_OF - timedelta(minutes=index), quantity=index)

    deliveries = await replay(migrated).read(limit=10)

    assert [d.envelope.sequence for d in deliveries] == [1, 2, 3, 4, 5]
    assert [d.envelope.payload["quantity"] for d in deliveries] == [0, 1, 2, 3, 4]


@pytest.mark.usefixtures("truncated_after_test")
async def test_reading_advances_a_cursor_rather_than_repeating(
    migrated: AsyncEngine,
) -> None:
    """A replay is one pass over history; re-offering would make it an infinite one."""
    await emptied(migrated)
    for _ in range(4):
        await record(migrated, recorded_at=AS_OF)
    stream = replay(migrated)

    first = await stream.read(limit=2)
    second = await stream.read(limit=2)

    assert [d.envelope.sequence for d in first] == [1, 2]
    assert [d.envelope.sequence for d in second] == [3, 4]
    assert await stream.read(limit=2) == []


@pytest.mark.usefixtures("truncated_after_test")
async def test_remaining_counts_what_is_still_ahead(migrated: AsyncEngine) -> None:
    """A replay is finite, and a caller wants to know how far through it is.

    Deliberately not on the port: live delivery has no such number, and offering
    one through the port would be a transport fact a consumer could branch on.
    """
    await emptied(migrated)
    for _ in range(3):
        await record(migrated, recorded_at=AS_OF)
    await record(migrated, recorded_at=AS_OF + timedelta(hours=1))
    stream = replay(migrated)

    assert await stream.remaining() == 3, "the row beyond the bound is not counted"

    await stream.read(limit=2)
    assert await stream.remaining() == 1


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_non_positive_limit_is_refused(migrated: AsyncEngine) -> None:
    """A zero-length read would do nothing and look like an exhausted replay."""
    with pytest.raises(ValidationError):
        await replay(migrated).read(limit=0)


# --------------------------------------------------------------------------- #
# Acknowledgement
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_acknowledging_clears_what_reading_made_pending(
    migrated: AsyncEngine,
) -> None:
    """The same lag signal the live adapter reports, from a different source."""
    await emptied(migrated)
    await record(migrated, recorded_at=AS_OF)
    await record(migrated, recorded_at=AS_OF)
    stream = replay(migrated)

    deliveries = await stream.read(limit=10)
    assert await stream.pending() == 2

    assert await stream.acknowledge([d.ack_token for d in deliveries]) == 2
    assert await stream.pending() == 0


@pytest.mark.usefixtures("truncated_after_test")
async def test_acknowledging_twice_reports_nothing_the_second_time(
    migrated: AsyncEngine,
) -> None:
    """Matching the live adapter exactly, so a consumer cannot tell them apart."""
    await emptied(migrated)
    await record(migrated, recorded_at=AS_OF)
    stream = replay(migrated)
    [delivery] = await stream.read(limit=10)

    assert await stream.acknowledge([delivery.ack_token]) == 1
    assert await stream.acknowledge([delivery.ack_token]) == 0


@pytest.mark.usefixtures("truncated_after_test")
async def test_acknowledging_writes_nothing_to_the_outbox(migrated: AsyncEngine) -> None:
    """A replay reads history; it must not edit the record it is reading.

    Marking a row published would make a backtest indistinguishable from a live
    relay run, and the next real relay pass would skip events it never sent.
    """
    await emptied(migrated)
    await record(migrated, recorded_at=AS_OF)
    stream = replay(migrated)
    [delivery] = await stream.read(limit=10)

    await stream.acknowledge([delivery.ack_token])

    async with migrated.connect() as connection:
        published = await connection.scalar(
            text("SELECT count(*) FROM outbox WHERE published_at IS NOT NULL")
        )
    assert published == 0


async def test_a_token_this_stream_did_not_issue_is_refused(migrated: AsyncEngine) -> None:
    """Ignoring it would leave a delivery pending forever and report a smaller count."""
    with pytest.raises(ValidationError):
        await replay(migrated).acknowledge(["1"])


@pytest.mark.usefixtures("truncated_after_test")
async def test_the_ack_token_is_the_streams_own_type(migrated: AsyncEngine) -> None:
    """Opaque to a consumer, which must not read the ordinal out of it (ADR-067)."""
    await emptied(migrated)
    await record(migrated, recorded_at=AS_OF)

    [delivery] = await replay(migrated).read(limit=10)

    assert isinstance(delivery.ack_token, ReplayAck)


# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("truncated_after_test")
async def test_an_envelope_version_this_consumer_cannot_read_is_refused(
    migrated: AsyncEngine,
) -> None:
    """ADR-061, applied identically to replay and to live delivery.

    A backtest that silently mis-read an event whose schema had moved on would
    produce a result nobody could tell was wrong.
    """
    await emptied(migrated)
    await record(migrated, recorded_at=AS_OF, version=2)

    with pytest.raises(UnsupportedEventVersionError):
        await replay(migrated, supported_versions=frozenset({1})).read(limit=10)


@pytest.mark.usefixtures("truncated_after_test")
async def test_a_supported_version_passes_through_unchanged(
    migrated: AsyncEngine,
) -> None:
    """The check must not alter what it accepts."""
    await emptied(migrated)
    event_id = await record(migrated, recorded_at=AS_OF)

    [delivery] = await replay(migrated, supported_versions=frozenset({1})).read(limit=10)

    assert delivery.envelope.event_id == event_id
    assert delivery.envelope.payload["quantity"] == 5
