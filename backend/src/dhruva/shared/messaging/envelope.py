"""The event envelope and its canonical wire form (ADR-002, ADR-061).

ADR-002 fixed nine fields in Phase 0. ADR-061 adds ``aggregate_type`` and
``sequence``, and pins the encoding.

Why canonical JSON
------------------
Replay must be deterministic (ADR-069): the same event replayed twice must
produce the same bytes, or two runs cannot be compared and an envelope cannot be
hashed. Python gives no such guarantee by default -- ``json.dumps`` preserves
insertion order and pads separators -- so the encoder pins sorted keys, tight
separators, no ASCII escaping and no non-finite values. That costs nothing now
and cannot be retrofitted once events are stored.

Why the envelope is frozen
--------------------------
An envelope is a record of something that already happened. A mutable one invites
a consumer to "correct" a field in flight, at which point two consumers disagree
about the past and neither is wrong. The ``event_id`` in particular is the
deduplication key (ADR-065): if it can change, deduplication is a suggestion.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Self
from uuid import UUID

from dhruva.shared.errors import ValidationError
from dhruva.shared.invariants import invariant
from dhruva.shared.messaging.codecs import decode_value, encode_value

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

__all__ = ["EventEnvelope", "UnsupportedEventVersionError", "canonical_json"]

#: Version of a payload schema. Starts at 1 and increments on an incompatible
#: change; additive optional fields do not bump it (ADR-061).
INITIAL_EVENT_VERSION: Final = 1

#: Field order is irrelevant on the wire -- keys are sorted -- but this tuple is
#: what a decoder requires to be present, and naming it once keeps the check and
#: the record from drifting apart.
REQUIRED_FIELDS: Final[tuple[str, ...]] = (
    "event_id",
    "event_type",
    "event_version",
    "aggregate_type",
    "aggregate_id",
    "sequence",
    "occurred_at",
    "recorded_at",
    "account_id",
    "correlation_id",
    "causation_id",
    "payload",
)


class UnsupportedEventVersionError(ValidationError):
    """Raised when an envelope declares a schema version a consumer cannot read.

    Deliberately not a silent skip and not a best-effort parse. A consumer that
    guesses at an unknown version is a consumer that changes trading behaviour on
    a schema change nobody reviewed (ADR-022, fail closed). The caller routes it
    to the dead-letter queue (ADR-064).
    """


def canonical_json(value: Mapping[str, Any]) -> str:
    """Serialise a mapping to canonical JSON.

    Sorted keys, no whitespace, no ASCII escaping, no NaN or Infinity. Two calls
    with equal content produce byte-identical output, which is what makes an
    envelope hashable and a replay comparable.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class EventEnvelope:
    """One event, as it travels between processes.

    Frozen and slotted: an envelope describes something that already happened, and
    a mutable record of the past is a record two consumers can disagree about.

    Attributes
    ----------
    event_id
        Stable and immutable for the life of the event. The deduplication key
        (ADR-065), so a consumer can recognise a redelivery.
    event_type
        Namespaced routing name, e.g. ``marketdata.bar.closed``.
    event_version
        Schema version of :attr:`payload`. A consumer that meets a version it does
        not understand raises rather than guessing.
    aggregate_type, aggregate_id
        What this happened to. ``aggregate_id`` may be absent: not every event
        belongs to an aggregate. When present it is the ordering key -- events for
        one aggregate are delivered in ``sequence`` order (ADR-062).
    sequence
        Monotonic within an aggregate. The replay anchor.
    occurred_at
        When the fact became true in the market.
    recorded_at
        When the platform learned it. Distinct from ``occurred_at`` whenever data
        arrives late, which is most of the time in market data; conflating them is
        how lookahead bias enters a backtest (ADR-007), and the pair is what makes
        the ``as_of`` bound of ADR-069 enforceable.
    account_id
        Tenant scope (ADR-004).
    correlation_id
        Ties everything caused by one external stimulus. Flat and shared.
    causation_id
        The ``event_id`` of the direct parent. A tree, not a tag -- ADR-018
        requires provenance to be a causal chain, and a flat correlation cannot
        say "this order exists because of that signal".
    payload
        Domain data, encoded by the exact codecs in :mod:`.codecs`.
    """

    event_id: UUID
    event_type: str
    event_version: int
    aggregate_type: str
    aggregate_id: UUID | None
    sequence: int
    occurred_at: datetime
    recorded_at: datetime
    account_id: UUID | None
    correlation_id: UUID
    causation_id: UUID | None
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        """Reject an envelope that could not be ordered, versioned or deduplicated."""
        invariant(
            self.event_version >= INITIAL_EVENT_VERSION,
            "event_version must be at least 1",
            event_version=self.event_version,
        )
        invariant(
            self.sequence >= 0,
            "sequence must be non-negative",
            sequence=self.sequence,
        )
        for name in ("occurred_at", "recorded_at"):
            value: datetime = getattr(self, name)
            invariant(
                value.tzinfo is not None and value.utcoffset() is not None,
                f"{name} must be timezone-aware",
                field=name,
                value=value.isoformat(),
            )

    def to_json(self) -> str:
        """Return the canonical wire form.

        Deterministic: equal envelopes serialise to identical bytes, on any run
        and in any process.
        """
        return canonical_json(self.to_mapping())

    def to_mapping(self) -> dict[str, Any]:
        """Return the envelope as JSON-ready primitives, without serialising."""
        return {
            "event_id": encode_value(self.event_id),
            "event_type": self.event_type,
            "event_version": self.event_version,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": encode_value(self.aggregate_id),
            "sequence": self.sequence,
            "occurred_at": encode_value(self.occurred_at),
            "recorded_at": encode_value(self.recorded_at),
            "account_id": encode_value(self.account_id),
            "correlation_id": encode_value(self.correlation_id),
            "causation_id": encode_value(self.causation_id),
            "payload": {key: encode_value(item) for key, item in self.payload.items()},
        }

    @classmethod
    def from_json(cls, document: str, *, supported_versions: frozenset[int] | None = None) -> Self:
        """Rebuild an envelope from its canonical wire form.

        Parameters
        ----------
        document
            Canonical JSON produced by :meth:`to_json`.
        supported_versions
            Versions this consumer understands. When given, an envelope declaring
            anything else raises :class:`UnsupportedEventVersionError` rather than
            being parsed on the assumption that the payload is close enough.

        Raises
        ------
        ValidationError
            If the document is not an object, or omits a required field.
        UnsupportedEventVersionError
            If the declared version is outside ``supported_versions``.
        """
        try:
            raw = json.loads(document)
        except json.JSONDecodeError as error:
            raise ValidationError("envelope is not valid JSON", reason=str(error)) from error

        if not isinstance(raw, dict):
            raise ValidationError("envelope must be a JSON object", actual_type=type(raw).__name__)

        missing = [name for name in REQUIRED_FIELDS if name not in raw]
        if missing:
            raise ValidationError("envelope is missing required field(s)", missing=missing)

        version = raw["event_version"]
        if supported_versions is not None and version not in supported_versions:
            raise UnsupportedEventVersionError(
                "envelope declares a schema version this consumer does not understand",
                event_version=version,
                supported=sorted(supported_versions),
                event_type=raw.get("event_type"),
            )

        return cls(
            event_id=_as_uuid(raw["event_id"], "event_id"),
            event_type=str(raw["event_type"]),
            event_version=int(version),
            aggregate_type=str(raw["aggregate_type"]),
            aggregate_id=_as_optional_uuid(raw["aggregate_id"]),
            sequence=int(raw["sequence"]),
            occurred_at=decode_value(raw["occurred_at"]),
            recorded_at=decode_value(raw["recorded_at"]),
            account_id=_as_optional_uuid(raw["account_id"]),
            correlation_id=_as_uuid(raw["correlation_id"], "correlation_id"),
            causation_id=_as_optional_uuid(raw["causation_id"]),
            payload={key: decode_value(item) for key, item in raw["payload"].items()},
        )


def _as_uuid(value: object, field: str) -> UUID:
    """Decode a required identifier, refusing a null."""
    decoded = decode_value(value)
    if not isinstance(decoded, UUID):
        raise ValidationError(
            "envelope field must be a UUID", field=field, actual_type=type(decoded).__name__
        )
    return decoded


def _as_optional_uuid(value: object) -> UUID | None:
    """Decode an optional identifier."""
    return None if value is None else decode_value(value)
