"""Half-open ranges, and the tiling property that motivates them."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dhruva.shared.errors import InvariantViolation
from dhruva.shared.time import DateRange, TimeRange

pytestmark = pytest.mark.unit


def test_a_date_range_excludes_its_end() -> None:
    """The convention, asserted directly."""
    span = DateRange(date(2026, 1, 1), date(2026, 1, 4))

    assert date(2026, 1, 1) in span
    assert date(2026, 1, 3) in span
    assert date(2026, 1, 4) not in span
    assert span.days == 3


@given(
    start=st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 1, 1)),
    length=st.integers(0, 500),
    split=st.integers(0, 500),
)
def test_consecutive_ranges_tile_without_gap_or_overlap(
    start: date, length: int, split: int
) -> None:
    """The property the half-open convention exists for.

    A closed range would contain the boundary date twice, which is how a bar gets
    counted in two windows and a backtest quietly reports more trades than
    occurred.
    """
    boundary = start + timedelta(days=min(split, length))
    end = start + timedelta(days=length)
    first, second = DateRange(start, boundary), DateRange(boundary, end)

    assert not first.overlaps(second)
    assert first.days + second.days == DateRange(start, end).days


def test_an_inverted_range_is_refused() -> None:
    """Not normalised by swapping.

    A caller passing bounds the wrong way round has a bug, and silently
    correcting it would hide the bug while returning a range they did not ask for.
    """
    with pytest.raises(InvariantViolation, match="must not be after"):
        DateRange(date(2026, 1, 4), date(2026, 1, 1))


def test_an_empty_range_contains_nothing() -> None:
    """A zero-length half-open range is legitimate and empty."""
    span = DateRange(date(2026, 1, 1), date(2026, 1, 1))

    assert span.is_empty
    assert span.days == 0
    assert date(2026, 1, 1) not in span
    assert list(span) == []


def test_iterating_yields_every_date_ascending() -> None:
    """Used for backfill planning and gap detection."""
    span = DateRange(date(2026, 1, 1), date(2026, 1, 4))

    assert list(span) == [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]


@given(
    a_start=st.integers(0, 100),
    a_len=st.integers(0, 50),
    b_start=st.integers(0, 100),
    b_len=st.integers(0, 50),
)
def test_intersection_is_contained_in_both(
    a_start: int, a_len: int, b_start: int, b_len: int
) -> None:
    """Whatever the overlap is, it belongs to both ranges."""
    base = date(2026, 1, 1)
    a = DateRange(base + timedelta(days=a_start), base + timedelta(days=a_start + a_len))
    b = DateRange(base + timedelta(days=b_start), base + timedelta(days=b_start + b_len))

    overlap = a.intersection(b)

    for day in overlap:
        assert day in a
        assert day in b


def test_overlap_is_symmetric() -> None:
    """Order of arguments cannot change the answer."""
    a = DateRange(date(2026, 1, 1), date(2026, 1, 10))
    b = DateRange(date(2026, 1, 5), date(2026, 1, 15))

    assert a.overlaps(b) == b.overlaps(a)


def test_adjacent_ranges_do_not_overlap() -> None:
    """The tiling property, stated at its boundary."""
    a = DateRange(date(2026, 1, 1), date(2026, 1, 5))
    b = DateRange(date(2026, 1, 5), date(2026, 1, 10))

    assert not a.overlaps(b)


def test_a_time_range_measures_duration() -> None:
    """Used for session length and latency windows."""
    span = TimeRange(
        datetime(2026, 7, 28, 3, 45, tzinfo=UTC),
        datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
    )

    assert span.duration == timedelta(hours=6, minutes=15)


def test_a_time_range_rejects_naive_bounds() -> None:
    """Comparing naive to aware raises in Python; better to fail at construction."""
    with pytest.raises(InvariantViolation, match="timezone-aware"):
        TimeRange(
            datetime(2026, 7, 28, 3, 45),  # noqa: DTZ001 - asserting rejection
            datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
        )


def test_ranges_render_in_half_open_notation() -> None:
    """The notation makes the convention visible wherever a range is logged."""
    assert str(DateRange(date(2026, 1, 1), date(2026, 1, 4))) == ("[2026-01-01, 2026-01-04)")


def test_ranges_are_frozen_and_hashable() -> None:
    """Value-object semantics; ranges are used as cache keys."""
    span = DateRange(date(2026, 1, 1), date(2026, 1, 4))

    assert hash(span) == hash(DateRange(date(2026, 1, 1), date(2026, 1, 4)))
    with pytest.raises((AttributeError, TypeError)):
        span.start = date(2020, 1, 1)  # type: ignore[misc]


def test_a_time_range_excludes_its_end() -> None:
    """The same half-open convention, applied to instants."""
    span = TimeRange(
        datetime(2026, 7, 28, 3, 45, tzinfo=UTC),
        datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
    )

    assert span.start in span
    assert span.end not in span
    assert span.end - timedelta(microseconds=1) in span


def test_an_empty_time_range_contains_nothing() -> None:
    """A zero-length span is legitimate and empty."""
    instant = datetime(2026, 7, 28, 3, 45, tzinfo=UTC)
    span = TimeRange(instant, instant)

    assert span.is_empty
    assert span.duration == timedelta(0)
    assert instant not in span


def test_time_ranges_overlap_symmetrically_and_tile_cleanly() -> None:
    """Consecutive sessions must not double-count the boundary instant."""
    a = TimeRange(
        datetime(2026, 7, 28, 3, 45, tzinfo=UTC),
        datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
    )
    b = TimeRange(
        datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
        datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
    )
    overlapping = TimeRange(
        datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
        datetime(2026, 7, 28, 11, 0, tzinfo=UTC),
    )

    assert not a.overlaps(b)
    assert a.overlaps(overlapping) == overlapping.overlaps(a)


def test_time_range_intersection() -> None:
    """Used to clip a query window to an available data window."""
    a = TimeRange(
        datetime(2026, 7, 28, 3, 45, tzinfo=UTC),
        datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
    )
    b = TimeRange(
        datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
        datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
    )

    assert a.intersection(b) == TimeRange(
        datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
        datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
    )
    assert a.intersection(b) == b.intersection(a)


def test_disjoint_time_ranges_intersect_to_empty() -> None:
    """An empty intersection is a legitimate answer, not an error."""
    a = TimeRange(datetime(2026, 7, 28, 3, 0, tzinfo=UTC), datetime(2026, 7, 28, 4, 0, tzinfo=UTC))
    b = TimeRange(datetime(2026, 7, 28, 9, 0, tzinfo=UTC), datetime(2026, 7, 28, 10, 0, tzinfo=UTC))

    assert a.intersection(b).is_empty


def test_an_inverted_time_range_is_refused() -> None:
    """Same reasoning as DateRange: a caller with reversed bounds has a bug."""
    with pytest.raises(InvariantViolation, match="must not be after"):
        TimeRange(
            datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
            datetime(2026, 7, 28, 3, 45, tzinfo=UTC),
        )


def test_time_ranges_render_in_half_open_notation() -> None:
    """Visible wherever a window is logged."""
    span = TimeRange(
        datetime(2026, 7, 28, 3, 45, tzinfo=UTC),
        datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
    )

    assert str(span).startswith("[2026-07-28T03:45:00+00:00,")
    assert str(span).endswith(")")
