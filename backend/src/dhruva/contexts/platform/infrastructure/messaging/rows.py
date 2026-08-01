"""Rebuilding an :class:`EventEnvelope` from an outbox row (ADR-061, ADR-062).

Shared by the relay and by replay, and shared deliberately. Two copies of this
translation would drift, and the drift would be the exact failure ADR-067 exists
to prevent: a consumer seeing one thing live and another in a backtest, with
nothing to say which was wrong.

What ``sequence`` means
-----------------------
``EventEnvelope.sequence`` is the **platform's** global ordinal, and today that
ordinal is the outbox primary key. The platform is the producer of every event
that passes through here, so its ordinal is the producer's, and it is copied --
never re-derived -- into the envelope. Redis stores the envelope's canonical JSON
verbatim and hands the value back unchanged; replay reads the same column. That
is what makes the two transports peers rather than two things that happen to
agree today.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from dhruva.shared.messaging import EventEnvelope, decode_value

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["envelope_from_row"]


def envelope_from_row(row: Any) -> EventEnvelope:
    """Rebuild an envelope from an outbox row.

    Parameters
    ----------
    row
        A row carrying the outbox's envelope columns. Typed loosely because it
        arrives from a driver cursor, whose rows are not statically described.

    Notes
    -----
    ``aggregate_type`` falls back to the event type only for rows written before
    migration 0005 added the column, and ``correlation_id`` to the event id only
    for rows written before 0003. New rows always carry both: a routing value
    that infrastructure infers routes events to a stream named after a guess
    (ADR-062).
    """
    payload: Mapping[str, Any] = json.loads(row.payload)
    return EventEnvelope(
        event_id=row.event_id,
        event_type=row.event_type,
        event_version=row.event_version or 1,
        aggregate_type=row.aggregate_type or row.event_type,
        aggregate_id=row.aggregate_id,
        sequence=row.sequence,
        occurred_at=row.occurred_at,
        recorded_at=row.recorded_at,
        account_id=row.account_id,
        correlation_id=row.correlation_id or row.event_id,
        causation_id=row.causation_id,
        payload={key: decode_value(value) for key, value in payload.items()},
    )
