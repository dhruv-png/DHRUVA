"""Identity authentication, session refresh and permission administration.

Everything here depends on protocols from ``domain.identity.ports``, so
nothing here imports SQLAlchemy, PyJWT or argon2 -- the layer contract forbids it
and, more usefully, it means both can be exercised against fakes without a
database while the same code runs against PostgreSQL in the integration suite.

That extends to the cipher. Broker-credential storage seals through a
``CredentialSealer`` port (ADR-077) rather than importing
``infrastructure.crypto``, so this package still imports nothing below it.
"""

from __future__ import annotations

from dhruva.contexts.platform.application.identity.authenticate import (
    AuthenticateUseCase,
    IssuedSession,
)
from dhruva.contexts.platform.application.identity.broker_credentials import (
    GetBrokerCredential,
    StoreBrokerCredential,
    StoreBrokerCredentialCommand,
    StoredCredential,
)
from dhruva.contexts.platform.application.identity.manage_permissions import (
    GrantPermissionUseCase,
    RevokePermissionUseCase,
)
from dhruva.contexts.platform.application.identity.refresh_session import RefreshSessionUseCase

__all__ = [
    "AuthenticateUseCase",
    "GetBrokerCredential",
    "GrantPermissionUseCase",
    "IssuedSession",
    "RefreshSessionUseCase",
    "RevokePermissionUseCase",
    "StoreBrokerCredential",
    "StoreBrokerCredentialCommand",
    "StoredCredential",
]
