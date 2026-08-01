"""Retry and failure classification for event delivery (ADR-064).

Pure policy. No I/O, no clock reading, no transport -- everything here is a
function of its arguments, so the relay's timing behaviour is testable without a
database, a broker or a sleeping test.

Two decisions live here.

**When to try again.** Exponential backoff with full jitter. Without jitter, a
broker outage produces a synchronised retry storm the moment it recovers, which
is how an outage extends itself. The delay is *returned*, never slept: ADR-069
requires a replay to advance a virtual clock rather than wait, and a policy that
slept would make a year of replay take weeks.

**Whether to try again at all.** A broker that is briefly unreachable deserves
twelve attempts. A malformed payload deserves none -- retrying it twelve times is
twelve identical failures and ten minutes of delay for a message that will never
succeed. The split follows the ADR-038 taxonomy, which already distinguishes
these.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from dhruva.shared.errors import (
    ExternalServiceError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
    ValidationError,
)

if TYPE_CHECKING:
    from datetime import datetime

__all__ = [
    "MAX_DELIVERY_ATTEMPTS",
    "Disposition",
    "RetryPolicy",
    "classify",
]

#: Attempts before an envelope is dead-lettered. Twelve, matching what S04
#: already declared: with the cap below it is roughly ten minutes of retry, which
#: is a defensible survival window for a broker blip. The tuned parameter is the
#: cap, not the count.
MAX_DELIVERY_ATTEMPTS: Final = 12


class Disposition(StrEnum):
    """What to do with a failed publication."""

    RETRY = "retry"
    """Transient. Schedule another attempt."""

    DEAD_LETTER = "dead_letter"
    """Terminal, or out of attempts. Move it aside (ADR-064)."""


#: Failures a wait could plausibly cure. Everything else is terminal: a malformed
#: payload is not going to become well-formed on the fourth attempt.
_RETRYABLE: Final[tuple[type[Exception], ...]] = (
    UpstreamUnavailableError,
    UpstreamTimeoutError,
    ExternalServiceError,
    ConnectionError,
    TimeoutError,
    OSError,
)

#: Failures that are the *producer's* fault and will recur identically. Listed
#: explicitly rather than inferred, so that adding an error type is a decision
#: rather than an accident of inheritance.
_TERMINAL: Final[tuple[type[Exception], ...]] = (ValidationError,)

#: Ceiling on the doubling exponent. Not a tuning parameter: with any base above
#: a microsecond, ``2**64`` seconds already exceeds every conceivable cap by many
#: orders of magnitude, so clamping here changes no delay anyone would configure.
#:
#: It exists because ``2 ** attempts`` for a large ``attempts`` produces an
#: integer that cannot be converted to a float, and Python raises ``OverflowError``
#: rather than saturating at infinity. ``attempts`` is read from a database
#: column and re-queued dead letters carry theirs intact (ADR-064), so it is not
#: this module's to assume small. Found by a property test at exactly 1024, which
#: is where a float's exponent runs out -- not by review, and not by any example
#: anyone would have thought to write.
_MAX_EXPONENT: Final = 64


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with full jitter.

    Attributes
    ----------
    base
        Delay before the first retry.
    cap
        Ceiling on any single delay. Without one, attempt twelve would wait over
        an hour and the outbox would look stalled.
    max_attempts
        Attempts before an envelope is dead-lettered.
    """

    base: timedelta = timedelta(seconds=1)
    cap: timedelta = timedelta(seconds=60)
    max_attempts: int = MAX_DELIVERY_ATTEMPTS

    def delay_for(self, attempts: int, *, jitter: float = 1.0) -> timedelta:
        """Return the delay before the next attempt.

        Parameters
        ----------
        attempts
            How many attempts have already failed. Zero means the first failure
            has just occurred.
        jitter
            Fraction of the computed delay to actually wait, in ``[0, 1]``.
            Supplied rather than drawn internally so a test can pin it: a policy
            that reached for ``random`` inside would be untestable, and this
            module exists to be testable.

        Notes
        -----
        Full jitter -- uniform over ``[0, delay]`` rather than ``delay ± 10%`` --
        because the point is to *decorrelate* retries across relay instances, and
        a narrow band leaves them nearly as synchronised as no jitter at all.
        """
        exponent = min(max(attempts, 0), _MAX_EXPONENT)
        exponential = self.base.total_seconds() * (2**exponent)
        bounded = min(exponential, self.cap.total_seconds())
        return timedelta(seconds=bounded * jitter)

    def next_attempt_at(
        self, *, now: datetime, attempts: int, jitter: float | None = None
    ) -> datetime:
        """Return the instant of the next attempt, from an injected ``now``.

        The clock is passed in rather than read (ADR-011), which is what lets a
        replay advance virtual time instead of waiting.
        """
        chosen = random.random() if jitter is None else jitter  # noqa: S311 - not cryptographic
        return now + self.delay_for(attempts, jitter=chosen)


def classify(error: Exception, *, attempts: int, policy: RetryPolicy) -> Disposition:
    """Decide whether a failed publication is retried or dead-lettered.

    Parameters
    ----------
    error
        What the transport reported.
    attempts
        How many attempts have already failed, *including* this one.
    policy
        Supplies the attempt ceiling.

    Notes
    -----
    Terminal failures skip the retry budget entirely. Spending twelve attempts
    and ten minutes on a payload that cannot parse delays every well-behaved
    event behind it for no possible gain.
    """
    if isinstance(error, _TERMINAL):
        return Disposition.DEAD_LETTER
    if attempts >= policy.max_attempts:
        return Disposition.DEAD_LETTER
    if isinstance(error, _RETRYABLE):
        return Disposition.RETRY
    # An unrecognised error is retried rather than discarded. Being wrong in this
    # direction costs a duplicate; being wrong in the other loses an event, and
    # ADR-062 is explicit about which of those is worse.
    return Disposition.RETRY
