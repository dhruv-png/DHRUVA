"""Error taxonomy.

Import errors from here rather than from the submodules::

    from dhruva.shared.errors import StaleDataError

See :mod:`dhruva.shared.errors.taxonomy` for the families and
:mod:`dhruva.shared.errors.base` for what every error carries.
"""

from __future__ import annotations

from dhruva.shared.errors.base import DhruvaError, ErrorCode
from dhruva.shared.errors.taxonomy import (
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    DataQualityError,
    DegradedModeError,
    ExternalServiceError,
    InvariantViolation,
    MissingDataError,
    NotFoundError,
    PermissionDeniedError,
    PreconditionUnknownError,
    RateLimitedError,
    SafetyError,
    StaleDataError,
    TokenExpiredError,
    TokenRevokedError,
    UnsafeConfigurationError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
    ValidationError,
)

__all__ = [
    "AuthenticationError",
    "ConfigurationError",
    "ConflictError",
    "DataQualityError",
    "DegradedModeError",
    "DhruvaError",
    "ErrorCode",
    "ExternalServiceError",
    "InvariantViolation",
    "MissingDataError",
    "NotFoundError",
    "PermissionDeniedError",
    "PreconditionUnknownError",
    "RateLimitedError",
    "SafetyError",
    "StaleDataError",
    "TokenExpiredError",
    "TokenRevokedError",
    "UnsafeConfigurationError",
    "UpstreamTimeoutError",
    "UpstreamUnavailableError",
    "ValidationError",
]
