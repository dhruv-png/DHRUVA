"""Domain events: immutable facts, not commands."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from dhruva.shared.errors import InvariantViolation
from dhruva.shared.events import DomainEvent
from dhruva.shared.identity import InstrumentId

pytestmark = pytest.mark.unit

OCCURRED = datetime(2026, 7, 28, 3, 45, tzinfo=UTC)


@dataclass(frozen=True, slots=True, kw_only=True)
class RegimeChanged(DomainEvent):
    """A representative subclass: a fact, in the past tense."""

    instrument_id: InstrumentId
    previous: str
    current: str


def _event(**overrides: object) -> RegimeChanged:
    defaults: dict[str, object] = {
        "occurred_at": OCCURRED,
        "instrument_id": InstrumentId.deterministic("NSE", "NIFTY"),
        "previous": "trending",
        "current": "choppy",
    }
    return RegimeChanged(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_an_event_carries_its_facts() -> None:
    """The subclass adds the fields describing what happened, and nothing else."""
    event = _event()

    assert event.previous == "trending"
    assert event.current == "choppy"
    assert event.occurred_at == OCCURRED


def test_event_type_is_derived_from_the_class() -> None:
    """Derived rather than stored, so it cannot disagree with the class it names.

    A stored ``event_type`` reading ``OrderFilled`` on an ``OrderRejected``
    instance is the kind of defect that survives review.
    """
    assert _event().event_type == "RegimeChanged"


def test_each_occurrence_gets_a_unique_identifier() -> None:
    """So a consumer can deduplicate without the producer thinking about it."""
    assert _event().event_id != _event().event_id


def test_an_explicit_event_id_is_honoured() -> None:
    """Replay and reconstruction need to preserve the original identity."""
    known = uuid.uuid4()

    assert _event(event_id=known).event_id == known


def test_events_are_immutable() -> None:
    """A fact that can be edited after the event is not a fact."""
    event = _event()

    with pytest.raises((AttributeError, TypeError)):
        event.current = "trending"  # type: ignore[misc]


def test_a_naive_timestamp_is_refused() -> None:
    """An event with no timezone has no defined position in the sequence."""
    with pytest.raises(InvariantViolation, match="timezone-aware"):
        _event(occurred_at=datetime(2026, 7, 28, 3, 45))  # noqa: DTZ001 - asserting rejection


def test_construction_is_keyword_only() -> None:
    """Events accumulate fields over years.

    Positional construction would turn every addition into a breaking change at
    every call site.
    """
    with pytest.raises(TypeError):
        RegimeChanged(OCCURRED, InstrumentId.new(), "a", "b")  # type: ignore[misc,call-arg,arg-type]


def test_occurred_at_is_distinct_from_when_it_was_recorded() -> None:
    """The two timestamps are different questions and must stay separate.

    ADR-007. Market data arrives late, and conflating when something *became
    true* with when we *learned it* is how lookahead bias enters a backtest.

    The base event carries only ``occurred_at``. Ingestion time belongs to the
    persistence layer's bitemporal columns (S04), not to the fact itself.
    """
    event = _event()
    recorded_later = event.occurred_at + timedelta(seconds=30)

    assert event.occurred_at < recorded_later
    assert not hasattr(event, "recorded_at")


def test_the_base_carries_no_transport_metadata() -> None:
    """Correlation and delivery belong to the envelope S05 wraps around a fact.

    Keeping them apart means a fact means the same thing whether it arrived over
    Redis Streams, was replayed from the ledger, or was built in a test.
    """
    fields = set(RegimeChanged.__dataclass_fields__)

    assert "correlation_id" not in fields
    assert "causation_id" not in fields
    assert "delivery_attempt" not in fields


def test_events_are_slotted() -> None:
    """A high-volume path; per-instance dicts would be a real cost by S10."""
    assert not hasattr(_event(), "__dict__")
