"""Structured logging.

Import from here::

    from dhruva.shared.logging import get_logger

    log = get_logger(__name__)
    log.info("subscription established", instrument_count=2847)

Never construct a logger directly from :mod:`structlog`: that bypasses the
redaction processor, which ADR-033 makes a control rather than a convenience.
"""

from __future__ import annotations

from dhruva.shared.logging.processors import SENSITIVE_KEY_PATTERN, make_redactor
from dhruva.shared.logging.setup import configure_logging, get_logger, logging_is_configured

__all__ = [
    "SENSITIVE_KEY_PATTERN",
    "configure_logging",
    "get_logger",
    "logging_is_configured",
    "make_redactor",
]
