"""The closed error taxonomy.

Eight families. A new family is an architectural decision requiring an ADR; a new
member within a family is ordinary work.

Family codes
------------
=========  ==========================================================
``CFG``    Configuration -- wrong, missing, or unsafe for the environment
``VAL``    Input validation -- the caller supplied something invalid
``NFD``    Not found -- the thing asked for does not exist
``CFL``    Conflict -- the operation contradicts current state
``PRM``    Permission -- the caller may not do this
``EXT``    External service -- an upstream failed, timed out, or throttled us
``DQL``    Data quality -- the data exists but cannot be trusted
``SAF``    Safety -- we stopped because we could not establish it was safe
=========  ==========================================================

The ``SAF`` family is the one worth pausing on. ADR-022 requires ambiguity to
fail closed, and giving "we do not know, so we stopped" its own family means the
Risk Engine (S25) can distinguish it from an ordinary failure, and the interface
can say so honestly rather than reporting a generic error. A safety error is not
a bug report; it is the system working.
"""

from __future__ import annotations

from dhruva.shared.errors.base import DhruvaError, ErrorCode

__all__ = [
    "ConfigurationError",
    "ConflictError",
    "DataQualityError",
    "DegradedModeError",
    "ExternalServiceError",
    "InvariantViolation",
    "MissingDataError",
    "NotFoundError",
    "PermissionDeniedError",
    "PreconditionUnknownError",
    "RateLimitedError",
    "SafetyError",
    "StaleDataError",
    "UnsafeConfigurationError",
    "UpstreamTimeoutError",
    "UpstreamUnavailableError",
    "ValidationError",
]


# --------------------------------------------------------------------------- #
# CFG -- configuration
# --------------------------------------------------------------------------- #


class ConfigurationError(DhruvaError):
    """Configuration is missing, malformed, or internally inconsistent.

    Raised during bootstrap, before the process is useful. Never retryable: the
    same configuration will fail the same way.
    """

    code = ErrorCode("DHR-CFG-001")


class UnsafeConfigurationError(ConfigurationError):
    """Configuration is valid in form but unsafe for the target environment.

    The case this exists for: a development default reaching production --
    debug enabled, a placeholder secret, a permissive origin list. Valid
    configuration that is wrong is more dangerous than invalid configuration,
    because nothing else will stop it.
    """

    code = ErrorCode("DHR-CFG-002")


# --------------------------------------------------------------------------- #
# VAL / NFD / CFL / PRM -- caller-facing
# --------------------------------------------------------------------------- #


class ValidationError(DhruvaError):
    """The caller supplied input that cannot be accepted."""

    code = ErrorCode("DHR-VAL-001")


class NotFoundError(DhruvaError):
    """The requested entity does not exist."""

    code = ErrorCode("DHR-NFD-001")


class ConflictError(DhruvaError):
    """The operation contradicts current state.

    Distinct from :class:`ValidationError`: the input was well-formed, but the
    world is not in a state where the operation makes sense.
    """

    code = ErrorCode("DHR-CFL-001")


class PermissionDeniedError(DhruvaError):
    """The caller is authenticated but not authorised for this operation.

    Named ``PermissionDeniedError`` rather than ``PermissionError`` to avoid
    shadowing the builtin, which would silently change the meaning of any
    ``except PermissionError`` written elsewhere.
    """

    code = ErrorCode("DHR-PRM-001")


# --------------------------------------------------------------------------- #
# EXT -- external services
# --------------------------------------------------------------------------- #


class ExternalServiceError(DhruvaError):
    """An upstream service failed.

    Retryable by default: most upstream failures are transient. Subclasses
    narrow the cause where the caller's response should differ.
    """

    code = ErrorCode("DHR-EXT-001")
    retryable = True


class UpstreamUnavailableError(ExternalServiceError):
    """The upstream could not be reached at all."""

    code = ErrorCode("DHR-EXT-002")


class UpstreamTimeoutError(ExternalServiceError):
    """The upstream accepted the request but did not answer in time.

    Deliberately distinct from :class:`UpstreamUnavailableError`. On the order
    path a timeout is *not* safely retryable -- the request may have succeeded --
    which is why ADR-015 requires idempotency keys rather than relying on this
    flag.
    """

    code = ErrorCode("DHR-EXT-003")


class RateLimitedError(ExternalServiceError):
    """The upstream refused the request because we exceeded its rate limit.

    Retryable, but only after backing off. Seeing this in normal operation means
    a rate governor is misconfigured, and it is an early-warning trigger for
    risk R04.
    """

    code = ErrorCode("DHR-EXT-004")


# --------------------------------------------------------------------------- #
# DQL -- data quality
# --------------------------------------------------------------------------- #


class DataQualityError(DhruvaError):
    """The data exists but cannot be trusted for the purpose at hand.

    Not a transport failure. The bytes arrived; they are unusable.
    """

    code = ErrorCode("DHR-DQL-001")


class StaleDataError(DataQualityError):
    """Data is older than the caller's freshness requirement.

    The most important error class in the platform's operational life. The
    ingest feed's failure mode is silence, not an exception (ADR-035): a stopped
    WebSocket looks exactly like a quiet market. This error is how that
    difference becomes visible.
    """

    code = ErrorCode("DHR-DQL-002")


class MissingDataError(DataQualityError):
    """Required data is absent for the requested range or entity.

    Distinct from :class:`NotFoundError`: the entity is known to exist, but the
    data covering it has a gap.
    """

    code = ErrorCode("DHR-DQL-003")


# --------------------------------------------------------------------------- #
# SAF -- safety (ADR-022, fail closed)
# --------------------------------------------------------------------------- #


class SafetyError(DhruvaError):
    """The operation was refused because safety could not be established.

    Not a failure of the operation -- a refusal to attempt it. ADR-022: in a
    financial system the cost of a false block is inconvenience, and the cost of
    a false proceed is capital.
    """

    code = ErrorCode("DHR-SAF-001")


class PreconditionUnknownError(SafetyError):
    """A precondition could not be evaluated, so the operation was blocked.

    The explicit "unknown" branch that ADR-022 requires every gate to have.
    Examples: ban-list state could not be fetched; margin could not be read;
    reconciliation has not run today.
    """

    code = ErrorCode("DHR-SAF-002")


class InvariantViolation(SafetyError):  # noqa: N818 - see docstring
    """A domain invariant does not hold.

    Defined here so the taxonomy is complete in one place; the canonical import
    is :class:`dhruva.shared.invariants.InvariantViolation`, which re-exports it
    alongside the :func:`~dhruva.shared.invariants.invariant` guard.

    Always a programming error: a value was constructed, or an aggregate
    mutated, into a state the domain model says cannot exist.

    Named without the conventional ``Error`` suffix on purpose. "InvariantError"
    reads as a failure of the invariant; the invariant is fine. What happened is
    that it was *violated*, and that distinction is worth the lint suppression --
    this name appears in every domain guard in the codebase.
    """

    code = ErrorCode("DHR-SAF-004")


class DegradedModeError(SafetyError):
    """The operation requires full capability and the process is degraded.

    Raised when the session is unauthenticated, the feed is stale, or the kill
    switch is tripped -- states in which the system knows it must not act.
    """

    code = ErrorCode("DHR-SAF-003")
