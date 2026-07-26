"""structlog processors.

The redaction processor is the one that matters. ADR-033 makes "no credential
reaches a log sink" a control rather than a convention, and a control that has
never been proven to fire is theatre -- so it is tested with a deliberate leak
test that fails the build.

Two strategies, because either alone is insufficient:

**By key.** Any field whose name matches a sensitive pattern is replaced. Cheap,
and covers the common case of a credential passed as a named field.

**By value.** Secrets constructed through :class:`~dhruva.shared.config.SecretValue`
register themselves; this processor scans rendered strings for those values. This
catches what key-based redaction always misses -- a credential interpolated into
a message, or carried in an exception's arguments.
"""

from __future__ import annotations

import re
from collections.abc import MutableMapping
from datetime import UTC, datetime
from typing import Any, Final

from dhruva.shared.config.secret import (
    REDACTED_PLACEHOLDER,
    registered_secret_values,
    secret_registry_version,
)
from dhruva.shared.context import current_context

__all__ = [
    "SENSITIVE_KEY_PATTERN",
    "add_correlation_context",
    "add_service_context",
    "add_utc_timestamp",
    "make_redactor",
]

EventDict = MutableMapping[str, Any]

#: Field names treated as sensitive regardless of content. Substring matching, so
#: ``kite_api_secret`` and ``db_password_hash`` are both caught. Deliberately
#: broad: a false positive costs one redacted debug line, a false negative costs
#: a credential rotation.
SENSITIVE_KEY_PATTERN: Final = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key"
    r"|authorization|auth[_-]?header|credential|totp|otp|session[_-]?id|cookie"
    r"|checksum|signature)",
    re.IGNORECASE,
)

#: Values shorter than this are not scanned for. Redacting every occurrence of a
#: four-character string would destroy ordinary log content.
_MIN_SCANNED_LENGTH: Final = 8


def add_utc_timestamp(_logger: object, _method_name: str, event_dict: EventDict) -> EventDict:
    """Stamp the record with an ISO-8601 UTC timestamp.

    ADR-006: storage and logs are UTC, display is Asia/Kolkata. An explicit
    offset is included so a log line is unambiguous when it is read six months
    later in a different timezone.
    """
    event_dict["timestamp"] = datetime.now(UTC).isoformat(timespec="milliseconds")
    return event_dict


def add_correlation_context(_logger: object, _method_name: str, event_dict: EventDict) -> EventDict:
    """Merge the bound correlation identifiers into the record.

    Unbound identifiers are omitted rather than rendered as null. Emitting
    ``correlation_id: null`` on every record of a process that never binds one is
    noise, and noise is what makes people stop reading logs.
    """
    event_dict.update(current_context().as_log_fields())
    return event_dict


def add_service_context(service: str, version: str, environment: str) -> Any:
    """Build a processor that stamps every record with service identity.

    Parameters
    ----------
    service
        Process name, for example ``dhruva-api``.
    version
        Distribution version, so a log line identifies the build that produced it.
    environment
        Deployment environment.

    Returns
    -------
    Any
        A structlog processor.
    """

    def processor(_logger: object, _method_name: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        event_dict.setdefault("version", version)
        event_dict.setdefault("environment", environment)
        return event_dict

    return processor


def _redact_value(value: Any, secrets: frozenset[str]) -> Any:
    """Recursively scrub registered secret values out of ``value``."""
    if isinstance(value, str):
        scrubbed = value
        for secret in secrets:
            if secret in scrubbed:
                scrubbed = scrubbed.replace(secret, REDACTED_PLACEHOLDER)
        return scrubbed
    if isinstance(value, dict):
        return {key: _redact_pair(key, item, secrets) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        rendered = [_redact_value(item, secrets) for item in value]
        return type(value)(rendered) if not isinstance(value, set) else set(rendered)
    return value


def _redact_pair(key: Any, value: Any, secrets: frozenset[str]) -> Any:
    """Redact by key name first, then by value."""
    if isinstance(key, str) and SENSITIVE_KEY_PATTERN.search(key):
        return REDACTED_PLACEHOLDER
    return _redact_value(value, secrets)


def make_redactor(*, scan_values: bool = True) -> Any:
    """Build the redaction processor.

    Parameters
    ----------
    scan_values
        Whether to scan rendered strings for registered secret values in addition
        to matching field names. Defaults to ``True``. Disabling it is a
        deliberate, reviewable act -- there is no configuration flag for it,
        because ADR-033 makes this a control rather than a preference.

    Returns
    -------
    Any
        A structlog processor.

    Notes
    -----
    Placed **last** in the chain, immediately before rendering, so it also
    catches secrets merged in by earlier processors -- from correlation context,
    from exception arguments, from anything that ran before it. That ordering is
    the design; a redactor earlier in the chain would miss exactly the cases that
    produce real leaks.

    The value snapshot is cached against a registry version counter, so a secret
    created after logging was configured is still scrubbed without rebuilding a
    set on every record. Measured: rebuilding per record cost roughly 40% of the
    total emission budget, which is exactly the kind of overhead that gets a
    security control switched off for a hot path (ADR-036).
    """
    cached_version = -1
    cached_secrets: frozenset[str] = frozenset()

    def processor(_logger: object, _method_name: str, event_dict: EventDict) -> EventDict:
        nonlocal cached_version, cached_secrets
        if scan_values:
            version = secret_registry_version()
            if version != cached_version:
                cached_secrets = frozenset(
                    value
                    for value in registered_secret_values()
                    if len(value) >= _MIN_SCANNED_LENGTH
                )
                cached_version = version
            secrets = cached_secrets
        else:
            secrets = frozenset()
        return {key: _redact_pair(key, value, secrets) for key, value in event_dict.items()}

    return processor
