"""The deployment environment, as a closed enum.

Four environments, matching plan section 17. The enum is closed because "which
environment am I in?" is asked in security-relevant branches -- whether to reject
a development default, whether to render logs as JSON, whether an exporter is
required -- and a typo in a string comparison would silently take the wrong one.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["Environment"]


class Environment(StrEnum):
    """Where this process is running.

    Attributes
    ----------
    LOCAL
        A developer's machine. Human-readable logs, permissive defaults.
    TEST
        Automated tests. Network blocked, no real credentials (plan section 14.3).
    STAGING
        Pre-production. Live market data, read-only broker credentials, simulated
        execution. Production-shaped in every respect that matters.
    PRODUCTION
        Live. From Gate G5 onward this is the only environment permitted to hold
        credentials with order permissions (ADR-026).
    """

    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_development(self) -> bool:
        """Return whether developer conveniences are acceptable here."""
        return self in {Environment.LOCAL, Environment.TEST}

    @property
    def is_deployed(self) -> bool:
        """Return whether this environment runs on shared infrastructure.

        Deployed environments must emit machine-readable logs, must reject
        development defaults, and must have observability exporters configured.
        Staging is included deliberately: a staging environment that is softer
        than production tests something other than production.
        """
        return self in {Environment.STAGING, Environment.PRODUCTION}

    @property
    def allows_live_orders(self) -> bool:
        """Return whether order placement may ever be enabled here.

        ``True`` only for :attr:`PRODUCTION`, and even there only once Gate G5
        has passed and the feature is explicitly enabled (ADR-026, ADR-028). This
        property answers "could it ever?", never "should it now?".
        """
        return self is Environment.PRODUCTION
