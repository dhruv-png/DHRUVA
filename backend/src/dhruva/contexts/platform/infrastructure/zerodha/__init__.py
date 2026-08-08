"""Zerodha-specific adapters owned by the Platform context.

Only authentication lives here. Market data and instrument discovery have their
own Kite adapters inside the contexts that own those questions (ADR-076); what
is here is the handshake, which belongs beside the credential store it fills.
"""

from __future__ import annotations

from dhruva.contexts.platform.infrastructure.zerodha.auth import (
    KITE_API_BASE,
    KITE_LOGIN_URL,
    KiteAuthenticator,
    parse_session_response,
)

__all__ = [
    "KITE_API_BASE",
    "KITE_LOGIN_URL",
    "KiteAuthenticator",
    "parse_session_response",
]
