"""Logging configuration.

One entry point, :func:`configure_logging`, called by :func:`dhruva.shared.runtime.bootstrap`
before anything else is allowed to log. One accessor, :func:`get_logger`.

There is no other sanctioned way to obtain a logger. ``print`` is banned by ruff
(``T20``), and a logger created directly from :mod:`structlog` would bypass the
redaction processor -- which is the whole point of routing everything through
here.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Final, Literal

import structlog

from dhruva.shared.logging.processors import (
    add_correlation_context,
    add_service_context,
    add_utc_timestamp,
    make_redactor,
)

__all__ = ["configure_logging", "get_logger", "logging_is_configured"]

_configured = False


class _StderrLogger:
    """A minimal logger that resolves ``sys.stderr`` at emit time.

    structlog's bundled ``PrintLogger`` binds its stream when the logger is
    constructed. That is fine in production and wrong everywhere else: a
    supervisor, a test harness or a capturing context manager that replaces
    ``sys.stderr`` afterwards would silently stop seeing output, and a logging
    system whose output cannot be captured is a logging system nobody can test.

    Resolving the stream per call costs an attribute lookup and buys
    testability, which for a security control (ADR-033) is not optional.
    """

    __slots__ = ()

    def msg(self, message: str) -> None:
        """Write one rendered record to the current standard error stream."""
        stream = sys.stderr
        stream.write(message + "\n")
        stream.flush()

    log = debug = info = warning = warn = error = critical = exception = fatal = msg

    def __repr__(self) -> str:  # pragma: no cover - debugger convenience only
        """Return a stable representation; the stream is resolved per call."""
        return "<dhruva stderr logger>"


def _stderr_logger_factory(*_args: object, **_kwargs: object) -> _StderrLogger:
    """Return a logger writing to the current standard error stream."""
    return _StderrLogger()


#: Loggers from third-party libraries that are noisy at INFO and say nothing
#: actionable. Raised to WARNING so real signals stay visible.
_NOISY_LOGGERS: Final = ("uvicorn.access", "urllib3.connectionpool", "asyncio")


def logging_is_configured() -> bool:
    """Return whether :func:`configure_logging` has run in this process."""
    return _configured


def configure_logging(  # noqa: PLR0913 - six independent, keyword-only settings; grouping them into an object would add a type without adding meaning
    *,
    level: str = "INFO",
    log_format: Literal["json", "console"] = "json",
    service: str = "dhruva",
    version: str = "0.0.0",
    environment: str = "local",
    scan_values: bool = True,
) -> None:
    """Configure structured logging for the process.

    Parameters
    ----------
    level
        Minimum level to emit.
    log_format
        ``json`` for machine consumption, ``console`` for a human at a terminal.
        Derived from the environment by
        :meth:`~dhruva.shared.config.settings.Settings.resolved_log_format`.
    service, version, environment
        Stamped onto every record, so a line identifies the build that produced it.
    scan_values
        Whether the redactor scans for registered secret values as well as
        matching field names.

    Notes
    -----
    The processor order is the design, not an implementation detail::

        contextvars → level → logger name → UTC timestamp → correlation
        → service identity → stack/exception rendering → REDACT → render

    Redaction runs last, immediately before rendering, so it sees everything the
    earlier processors merged in -- context, exception arguments, formatted
    strings. A redactor placed earlier would miss precisely the cases that
    produce real leaks.

    Calling this twice is safe and reconfigures cleanly; tests rely on that.
    """
    global _configured  # noqa: PLW0603 - process-wide configuration is inherently global

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        add_utc_timestamp,
        add_correlation_context,
        add_service_context(service=service, version=version, environment=environment),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        make_redactor(scan_values=scan_values),
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer(sort_keys=True)
        if log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=_stderr_logger_factory,
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level.upper(), force=True)
    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger.

    Parameters
    ----------
    name
        Conventionally ``__name__``, so a record identifies its origin module.

    Returns
    -------
    structlog.stdlib.BoundLogger
        A logger whose records pass through the redaction processor.

    Notes
    -----
    Obtaining a logger any other way bypasses redaction. That is why this is the
    only sanctioned accessor, and why ``print`` is banned by lint.
    """
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
