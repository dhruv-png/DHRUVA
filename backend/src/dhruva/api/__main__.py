"""``python -m dhruva.api`` -- run the observability component.

The composition root: the only place permitted to construct configuration, wire
adapters, and configure the tracing SDK.
"""

from __future__ import annotations

import uvicorn

from dhruva.api.app import create_app
from dhruva.shared.runtime import bootstrap

__all__ = ["main", "uvicorn"]


def main() -> None:
    """Bootstrap the runtime and serve the observability endpoints."""
    context = bootstrap(service="dhruva-api")
    uvicorn.run(
        create_app(context),
        host=context.settings.app.host,
        port=context.settings.app.port,
        log_config=None,  # structlog owns logging; uvicorn must not reconfigure it
        access_log=False,  # the correlation middleware emits richer records
    )


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
