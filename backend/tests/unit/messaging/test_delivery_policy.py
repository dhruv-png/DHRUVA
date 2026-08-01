"""Retry and classification policy (ADR-064).

This module is pure policy, so it is tested as pure policy: no clock, no broker,
no database, and nothing that sleeps. Every assertion below is a statement about
a function of its arguments.

Why this file exists at all is worth stating. The policy was previously exercised
only *through* the relay's integration tests, which means it was covered but not
tested: those tests assert on rows in a table, so a policy that returned a
plausible-but-wrong delay would still have produced a green suite. The arithmetic
is where the fault would hide, and the arithmetic is what is asserted here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dhruva.contexts.platform.domain.messaging import (
    MAX_DELIVERY_ATTEMPTS,
    Disposition,
    RetryPolicy,
    classify,
)
from dhruva.shared.errors import (
    ConfigurationError,
    ExternalServiceError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
    ValidationError,
)
from dhruva.shared.messaging import UnsupportedEventVersionError

pytestmark = pytest.mark.unit

NOW = datetime(2026, 7, 30, 9, 15, tzinfo=UTC)

#: A policy whose numbers are small and exact, so an assertion can name the
#: expected value rather than compute it the same way the code under test does --
#: which would only prove the expression was copied correctly.
FIXED = RetryPolicy(base=timedelta(seconds=1), cap=timedelta(seconds=60), max_attempts=12)


# --------------------------------------------------------------------------- #
# Backoff arithmetic
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("attempts", "seconds"),
    [(0, 1), (1, 2), (2, 4), (3, 8), (4, 16), (5, 32), (6, 60), (7, 60), (11, 60)],
)
def test_the_delay_doubles_until_it_reaches_the_cap(attempts: int, seconds: int) -> None:
    """Exponential to attempt six, flat thereafter. The cap is the tuned parameter."""
    assert FIXED.delay_for(attempts, jitter=1.0) == timedelta(seconds=seconds)


def test_a_negative_attempt_count_is_treated_as_the_first() -> None:
    """A caller cannot produce a negative delay by passing a negative count.

    Not defensive decoration: ``attempts`` arrives from a database column, and a
    negative there would otherwise yield a fractional delay that silently retries
    faster than the policy allows.
    """
    assert FIXED.delay_for(-5, jitter=1.0) == FIXED.delay_for(0, jitter=1.0)


def test_zero_jitter_retries_immediately_and_full_jitter_waits_the_whole_delay() -> None:
    """The two ends of the jitter range, pinned so the multiplication cannot invert."""
    assert FIXED.delay_for(3, jitter=0.0) == timedelta(0)
    assert FIXED.delay_for(3, jitter=1.0) == timedelta(seconds=8)


@given(
    attempts=st.integers(min_value=0, max_value=4096),
    jitter=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
def test_no_delay_ever_exceeds_the_cap(attempts: int, jitter: float) -> None:
    """The ceiling holds for every attempt count a row could carry.

    The upper bound is deliberately far beyond ``max_attempts``. A dead-lettered
    row that an operator re-queues (ADR-064) arrives with its attempt count
    intact, and a policy that only behaves for counts it expects is a policy that
    fails on the day someone intervenes.
    """
    assert timedelta(0) <= FIXED.delay_for(attempts, jitter=jitter) <= FIXED.cap


@given(attempts=st.integers(min_value=0, max_value=64))
def test_the_delay_never_decreases_as_attempts_accumulate(attempts: int) -> None:
    """Backoff backs off. A later attempt never waits less than an earlier one."""
    assert FIXED.delay_for(attempts, jitter=1.0) <= FIXED.delay_for(attempts + 1, jitter=1.0)


def test_the_whole_retry_window_is_about_seven_minutes() -> None:
    """The worst case a well-behaved event can spend in the outbox.

    Asserted as a number because it is a promise to whoever is watching a queue
    during an incident. The relay increments before classifying, so the first
    scheduled delay is ``delay_for(1)`` and the last is ``delay_for(11)``.
    """
    total = sum(
        (FIXED.delay_for(n, jitter=1.0) for n in range(1, FIXED.max_attempts)),
        start=timedelta(0),
    )
    assert timedelta(minutes=6) < total < timedelta(minutes=8)


def test_next_attempt_at_is_never_in_the_past() -> None:
    """Time moves forward regardless of the jitter drawn."""
    for _ in range(200):
        assert FIXED.next_attempt_at(now=NOW, attempts=3) >= NOW


def test_next_attempt_at_uses_the_injected_clock_rather_than_reading_one() -> None:
    """ADR-011. A policy that read a clock could not be replayed."""
    assert FIXED.next_attempt_at(now=NOW, attempts=0, jitter=1.0) == NOW + timedelta(seconds=1)


def test_the_policy_is_immutable() -> None:
    """A retry policy that a caller can mutate is a policy nobody can reason about."""
    with pytest.raises(AttributeError):
        FIXED.base = timedelta(seconds=99)  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "error",
    [
        UpstreamUnavailableError("broker down"),
        UpstreamTimeoutError("broker slow"),
        ExternalServiceError("broker refused"),
        ConnectionError("socket closed"),
        TimeoutError("no answer"),
        OSError("no route to host"),
    ],
)
def test_a_transient_failure_is_retried(error: Exception) -> None:
    """Anything a wait could cure gets another attempt."""
    assert classify(error, attempts=1, policy=FIXED) is Disposition.RETRY


@pytest.mark.parametrize(
    "error",
    [
        ValidationError("payload is malformed"),
        UnsupportedEventVersionError("consumer cannot read this version"),
    ],
)
def test_a_terminal_failure_is_dead_lettered_without_spending_the_budget(
    error: Exception,
) -> None:
    """Zero attempts spent. Twelve identical rejections help nobody (ADR-064)."""
    assert classify(error, attempts=0, policy=FIXED) is Disposition.DEAD_LETTER


def test_a_transient_failure_is_dead_lettered_once_the_budget_is_gone() -> None:
    """The ceiling is inclusive: reaching it is the last attempt, not the first extra one."""
    below = classify(UpstreamUnavailableError("down"), attempts=11, policy=FIXED)
    at = classify(UpstreamUnavailableError("down"), attempts=12, policy=FIXED)

    assert below is Disposition.RETRY
    assert at is Disposition.DEAD_LETTER


def test_an_unrecognised_error_is_retried_rather_than_discarded() -> None:
    """Being wrong this way costs a duplicate; the other way loses an event (ADR-062)."""
    assert classify(ConfigurationError("misconfigured"), attempts=0, policy=FIXED) is (
        Disposition.RETRY
    )
    assert classify(RuntimeError("something nobody anticipated"), attempts=0, policy=FIXED) is (
        Disposition.RETRY
    )


@given(attempts=st.integers(min_value=0, max_value=1000))
def test_terminal_beats_the_budget_at_every_attempt_count(attempts: int) -> None:
    """A malformed payload is dead-lettered whether it is the first failure or the fiftieth."""
    assert classify(ValidationError("bad"), attempts=attempts, policy=FIXED) is (
        Disposition.DEAD_LETTER
    )


def test_the_declared_attempt_ceiling_is_what_the_default_policy_uses() -> None:
    """The constant and the default must not drift apart; S04 already declared twelve."""
    assert RetryPolicy().max_attempts == MAX_DELIVERY_ATTEMPTS == 12
