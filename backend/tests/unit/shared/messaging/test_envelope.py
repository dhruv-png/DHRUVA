"""Envelope and codec properties (ADR-061, ADR-069).

Properties rather than examples, because the guarantee is universal: *every*
domain value survives the wire exactly, and *every* envelope serialises the same
way twice. Three worked examples would demonstrate neither.

The determinism tests are the ones that matter most. Replay compares two runs
byte for byte, so a serialiser that is merely *usually* stable produces a
backtest that is merely usually reproducible -- and the day it differs, nobody
will suspect the encoder.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dhruva.shared.errors import ValidationError
from dhruva.shared.invariants import InvariantViolation
from dhruva.shared.messaging import (
    EventEnvelope,
    UnencodableValueError,
    UnsupportedEventVersionError,
    canonical_json,
    decode_value,
    encode_value,
)
from dhruva.shared.money import Currency, Money, Price, Quantity, Ratio

pytestmark = pytest.mark.unit

OCCURRED = datetime(2026, 7, 28, 9, 15, tzinfo=UTC)
RECORDED = datetime(2026, 7, 28, 9, 20, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #


def _money() -> st.SearchStrategy[Money]:
    return st.integers(min_value=-(10**15), max_value=10**15).map(lambda n: Money(n, Currency.INR))


def _price() -> st.SearchStrategy[Price]:
    return st.integers(min_value=-(10**16), max_value=10**16).map(Price)


def _decimals() -> st.SearchStrategy[Decimal]:
    return st.decimals(min_value=Decimal("-1E9"), max_value=Decimal("1E9"), places=8)


def _instants() -> st.SearchStrategy[datetime]:
    return st.integers(min_value=0, max_value=10**9).map(
        lambda s: datetime(2020, 1, 1, tzinfo=UTC) + timedelta(seconds=s)
    )


def _domain_values() -> st.SearchStrategy[Any]:
    """Every type the codecs claim to support, plus the primitives around them."""
    return st.one_of(
        _money(),
        _price(),
        st.integers(min_value=0, max_value=10**7).map(Quantity),
        _decimals().map(Ratio),
        _decimals(),
        st.uuids(),
        _instants(),
        st.dates(),
        st.integers(),
        st.text(max_size=40),
        st.booleans(),
        st.none(),
    )


# --------------------------------------------------------------------------- #
# Codec round trips
# --------------------------------------------------------------------------- #


@given(value=_domain_values())
def test_every_supported_value_survives_the_wire_exactly(value: object) -> None:
    """The defining property. Exactness is what ADR-042 buys and this preserves."""
    assert decode_value(encode_value(value)) == value


@given(values=st.lists(_domain_values(), max_size=6))
def test_nesting_does_not_weaken_the_guarantee(values: list[object]) -> None:
    """A value inside a list inside a dict is still the value that went in."""
    payload = {"outer": {"inner": values}}

    assert decode_value(encode_value(payload)) == payload


@given(amount=_money())
def test_money_crosses_the_wire_as_an_integer_not_a_number(amount: Money) -> None:
    """The encoded form must carry minor units, never a formatted amount.

    A decimal string would round-trip too, which is exactly why this is asserted
    on the encoded shape rather than on the round trip: the point is that no
    parsing of a fractional literal happens anywhere.
    """
    encoded = encode_value(amount)

    assert encoded["minor_units"] == amount.minor_units
    assert isinstance(encoded["minor_units"], int)
    assert encoded["currency"] == "INR"


@pytest.mark.parametrize("bad", [1.5, 0.0, -2.75, float("inf")])
def test_a_float_is_refused_rather_than_coerced(bad: float) -> None:
    """There is no correct float representation of a monetary amount."""
    with pytest.raises(UnencodableValueError, match="float"):
        encode_value(bad)


def test_a_float_nested_deep_in_a_payload_is_still_refused() -> None:
    """The check is at every depth, because a payload is a tree."""
    with pytest.raises(UnencodableValueError, match="float"):
        encode_value({"a": [{"b": [1, 2, 3.5]}]})


def test_a_type_with_no_codec_is_refused_rather_than_stringified() -> None:
    """A silent str() fallback puts '<object at 0x…>' in a message."""

    class Unknown:
        pass

    with pytest.raises(UnencodableValueError, match="no codec"):
        encode_value(Unknown())


def test_a_naive_datetime_is_refused() -> None:
    """An instant with no offset has no defined position in a sequence (ADR-006)."""
    with pytest.raises(UnencodableValueError, match="timezone-aware"):
        encode_value(datetime(2026, 7, 28, 9, 15))  # noqa: DTZ001 - the point of the test


def test_an_unknown_encoded_type_is_refused_on_the_way_in() -> None:
    """A producer ahead of this consumer must not be guessed at (ADR-022)."""
    with pytest.raises(UnencodableValueError, match="unknown encoded type"):
        decode_value({"__t": "quaternion", "value": "1+2i"})


def test_a_malformed_encoded_body_is_refused() -> None:
    """The discriminator promised a shape the body does not have."""
    with pytest.raises(UnencodableValueError, match="malformed"):
        decode_value({"__t": "money", "minor_units": "not-a-number", "currency": "INR"})


# --------------------------------------------------------------------------- #
# Canonical JSON and determinism (ADR-069)
# --------------------------------------------------------------------------- #


@given(keys=st.lists(st.text(min_size=1, max_size=8), min_size=2, max_size=6, unique=True))
def test_key_insertion_order_does_not_change_the_bytes(keys: list[str]) -> None:
    """Two dicts with the same content serialise identically whatever their order.

    This is the property replay determinism rests on. Without sorted keys, two
    runs that build a payload in different orders produce different bytes, and a
    byte-level comparison of two backtests becomes noise.
    """
    forward = {key: index for index, key in enumerate(keys)}
    backward = {key: forward[key] for key in reversed(keys)}

    assert canonical_json(forward) == canonical_json(backward)


def test_canonical_json_is_compact_and_sorted() -> None:
    """Pinned explicitly, because a later 'tidy up' could reintroduce whitespace."""
    document = canonical_json({"b": 1, "a": 2})

    assert document == '{"a":2,"b":1}'


def test_canonical_json_refuses_non_finite_numbers() -> None:
    """NaN is not JSON, and a consumer in another language would reject it."""
    with pytest.raises(ValueError, match=r"Out of range|not JSON compliant"):
        canonical_json({"x": float("nan")})


# --------------------------------------------------------------------------- #
# Envelope
# --------------------------------------------------------------------------- #


def _envelope(**overrides: Any) -> EventEnvelope:
    fields: dict[str, Any] = {
        "event_id": uuid4(),
        "event_type": "trading.order.filled",
        "event_version": 1,
        "aggregate_type": "Order",
        "aggregate_id": uuid4(),
        "sequence": 7,
        "occurred_at": OCCURRED,
        "recorded_at": RECORDED,
        "account_id": uuid4(),
        "correlation_id": uuid4(),
        "causation_id": uuid4(),
        "payload": {"price": Price.parse("1234.5678"), "value": Money.parse("98765.43")},
    }
    fields.update(overrides)
    return EventEnvelope(**fields)


def test_an_envelope_round_trips_through_its_wire_form() -> None:
    """Everything that went in comes back, including the domain values."""
    original = _envelope()

    assert EventEnvelope.from_json(original.to_json()) == original


def test_serialising_the_same_envelope_twice_produces_identical_bytes() -> None:
    """Determinism at the envelope level, not just the encoder's."""
    envelope = _envelope()

    assert envelope.to_json() == envelope.to_json()


def test_two_envelopes_with_equal_content_serialise_identically() -> None:
    """Two runs of a replay build separate objects; their bytes must still match."""
    shared: dict[str, Any] = {
        "event_id": uuid4(),
        "aggregate_id": uuid4(),
        "account_id": uuid4(),
        "correlation_id": uuid4(),
        "causation_id": uuid4(),
    }

    assert _envelope(**shared).to_json() == _envelope(**shared).to_json()


def test_a_round_trip_does_not_perturb_the_bytes() -> None:
    """Decode then re-encode is the identity. Replay depends on it."""
    document = _envelope().to_json()

    assert EventEnvelope.from_json(document).to_json() == document


def test_an_envelope_is_immutable() -> None:
    """A record of the past that can be edited is one consumers can disagree about."""
    envelope = _envelope()

    with pytest.raises(AttributeError):
        envelope.sequence = 9  # type: ignore[misc]  # asserting runtime rejection


def test_an_optional_identifier_may_be_absent() -> None:
    """Not every event belongs to an aggregate, an account or a parent."""
    envelope = _envelope(aggregate_id=None, account_id=None, causation_id=None)

    restored = EventEnvelope.from_json(envelope.to_json())

    assert restored.aggregate_id is None
    assert restored.account_id is None
    assert restored.causation_id is None


@pytest.mark.parametrize("field", ["event_id", "sequence", "payload", "recorded_at"])
def test_a_missing_required_field_is_refused(field: str) -> None:
    """A truncated envelope must not decode into a partially-populated one."""
    document = json.loads(_envelope().to_json())
    del document[field]

    with pytest.raises(ValidationError, match="missing required field"):
        EventEnvelope.from_json(canonical_json(document))


def test_a_version_this_consumer_does_not_understand_is_refused() -> None:
    """Fail closed (ADR-022); the caller routes it to the DLQ (ADR-064)."""
    document = _envelope(event_version=3).to_json()

    with pytest.raises(UnsupportedEventVersionError, match="does not understand"):
        EventEnvelope.from_json(document, supported_versions=frozenset({1, 2}))


def test_a_supported_version_passes_the_check() -> None:
    """The guard must not refuse what it was told to accept."""
    document = _envelope(event_version=2).to_json()

    restored = EventEnvelope.from_json(document, supported_versions=frozenset({1, 2}))

    assert restored.event_version == 2


@pytest.mark.parametrize(("field", "value"), [("event_version", 0), ("sequence", -1)])
def test_an_unorderable_or_unversioned_envelope_is_refused(field: str, value: int) -> None:
    """Construction-time invariants, so a bad envelope never reaches the wire."""
    with pytest.raises(InvariantViolation):
        _envelope(**{field: value})


def test_a_naive_timestamp_is_refused_at_construction() -> None:
    """Both halves of the bitemporal pair must be orderable (ADR-006, ADR-007)."""
    with pytest.raises(InvariantViolation, match="timezone-aware"):
        _envelope(recorded_at=datetime(2026, 7, 28, 9, 20))  # noqa: DTZ001 - the point


def test_the_bitemporal_pair_is_preserved_independently() -> None:
    """`occurred_at` and `recorded_at` must not be conflated in transit.

    The whole `as_of` mechanism of ADR-069 rests on the two remaining distinct,
    so a codec that collapsed them would silently reintroduce lookahead bias.
    """
    envelope = _envelope(occurred_at=OCCURRED, recorded_at=RECORDED)

    restored = EventEnvelope.from_json(envelope.to_json())

    assert restored.occurred_at == OCCURRED
    assert restored.recorded_at == RECORDED
    assert restored.occurred_at != restored.recorded_at


def test_the_payload_keeps_exact_domain_types_not_their_reprs() -> None:
    """A Price must come back a Price, not the string that looks like one."""
    restored = EventEnvelope.from_json(_envelope().to_json())

    assert isinstance(restored.payload["price"], Price)
    assert isinstance(restored.payload["value"], Money)
    assert restored.payload["price"] == Price.parse("1234.5678")
    assert restored.payload["value"] == Money.parse("98765.43")


def test_identifiers_come_back_as_uuids() -> None:
    """A string that looks like a UUID is not a UUID to a consumer that indexes on it."""
    restored = EventEnvelope.from_json(_envelope().to_json())

    assert isinstance(restored.event_id, UUID)
    assert isinstance(restored.correlation_id, UUID)


def test_a_document_that_is_not_an_object_is_refused() -> None:
    """A JSON array is valid JSON and is not an envelope."""
    with pytest.raises(ValidationError, match="must be a JSON object"):
        EventEnvelope.from_json("[1, 2, 3]")


def test_invalid_json_is_refused_with_the_reason() -> None:
    """A truncated message should say so, not raise a bare decode error."""
    with pytest.raises(ValidationError, match="not valid JSON"):
        EventEnvelope.from_json("{not json")


def test_a_date_payload_survives_as_a_date() -> None:
    """`date` and `datetime` are distinct types and must stay distinct."""
    envelope = _envelope(payload={"day": date(2026, 7, 28)})

    restored = EventEnvelope.from_json(envelope.to_json())

    assert restored.payload["day"] == date(2026, 7, 28)
    assert not isinstance(restored.payload["day"], datetime)


@given(quantity=st.integers(min_value=0, max_value=10**7))
def test_quantity_and_ratio_survive_a_payload(quantity: int) -> None:
    """The remaining two domain types, exercised through the envelope."""
    envelope = _envelope(payload={"size": Quantity(quantity), "rate": Ratio(Decimal("0.00012345"))})

    restored = EventEnvelope.from_json(envelope.to_json())

    assert restored.payload["size"] == Quantity(quantity)
    assert restored.payload["rate"] == Ratio(Decimal("0.00012345"))
