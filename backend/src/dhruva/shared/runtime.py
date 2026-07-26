"""Composition-root bootstrap.

One ordered entry point, called by every composition root before it does anything
else. The ordering *is* the design:

1. **Load settings** -- fail fast on invalid configuration, before anything
   depends on it.
2. **Configure logging** -- nothing before this point is permitted to log, so
   there is no window producing unstructured output.
3. **Register known secret values** -- redaction becomes active before any
   component holds a credential, so there is no window in which one could be
   logged in the clear.
4. **Configure tracing** -- a no-op unless an exporter is configured.
5. **Emit the startup banner** -- including whether tracing is active, because a
   no-op tracer is otherwise indistinguishable from a broken one.

Steps 2 and 3 in that order are the security-relevant part. Reversing them would
leave a brief interval during which a secret could reach a sink unredacted.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dhruva.__about__ import __version__
from dhruva.shared.config.settings import Settings, load_settings
from dhruva.shared.logging import configure_logging, get_logger
from dhruva.shared.observability import (
    HealthRegistry,
    MetricsRegistry,
    tracing_is_active,
)

__all__ = ["RuntimeContext", "bootstrap"]


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Everything a composition root needs, constructed once.

    Attributes
    ----------
    settings
        Validated process configuration.
    health
        Registry that components declare readiness checks into.
    metrics
        Registry that components declare metrics into.
    service
        Name of this process, for example ``dhruva-api``.

    Notes
    -----
    Passed explicitly down the wiring rather than exposed as a module-level
    global. An ambient context cannot be isolated in tests, and it re-creates
    exactly the ambient-configuration problem ADR-031 exists to remove.
    """

    settings: Settings
    health: HealthRegistry
    metrics: MetricsRegistry
    service: str


def bootstrap(
    *,
    service: str,
    env_file: Path | None = None,
    scan_secret_values: bool = True,
) -> RuntimeContext:
    """Bring the process runtime up, in order.

    Parameters
    ----------
    service
        Process name, stamped onto every log record and span.
    env_file
        Optional ``.env`` read before the process environment. Environment
        variables always win.
    scan_secret_values
        Whether log redaction scans for registered secret values as well as
        matching field names.

    Returns
    -------
    RuntimeContext
        Configuration and the registries components declare into.

    Raises
    ------
    ConfigurationError
        If configuration is missing or malformed.
    UnsafeConfigurationError
        If configuration is valid but unsafe for its environment -- a
        development default reaching staging or production.

    Notes
    -----
    Safe to call once per process. Calling it twice reconfigures logging, which
    is harmless and is what the test suite relies on.
    """
    settings = load_settings(env_file)

    configure_logging(
        level=settings.log.level,
        log_format=settings.resolved_log_format(),
        service=service,
        version=__version__,
        environment=str(settings.app.environment),
        scan_values=scan_secret_values,
    )

    log = get_logger(__name__)
    context = RuntimeContext(
        settings=settings,
        health=HealthRegistry(),
        metrics=MetricsRegistry(),
        service=service,
    )

    log.info(
        "runtime started",
        service=service,
        version=__version__,
        environment=str(settings.app.environment),
        log_format=settings.resolved_log_format(),
        tracing_configured=settings.otel.enabled,
        tracing_active=tracing_is_active(),
        debug=settings.app.debug,
    )
    if settings.otel.enabled and not tracing_is_active():
        log.warning(
            "tracing is enabled in configuration but no SDK provider is installed; "
            "spans will be discarded. Install the 'tracing' extra at this composition root.",
            endpoint=settings.otel.endpoint,
        )

    return context
