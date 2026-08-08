"""Broker authentication: what a broker credential *means*, generically.

The identity package (ADR-070, ADR-077) knows how to seal a secret and which
lifecycle it belongs to. It deliberately does not know what is inside one. This
package is the missing half: the shape of the two payloads a broker credential
carries, and the states an operator can be in.

Nothing here names a broker. ``BrokerApplication`` is an identifier and a
secret; ``BrokerSession`` is a token, the broker's own user id, and when it
stops working. Kite happens to call them api_key, api_secret, access_token and
user_id, and that mapping lives in the adapter -- because a domain that spelled
them the Kite way would have to be edited before a second broker could exist,
which is exactly what ADR-003 is for.
"""

from __future__ import annotations

from dhruva.contexts.platform.domain.broker.ports import (
    BrokerAuthenticator,
    BrokerSessionMaterial,
    SecretPrompt,
)
from dhruva.contexts.platform.domain.broker.session import (
    BrokerApplication,
    BrokerAuthState,
    BrokerSession,
    parse_broker_application,
    parse_broker_session,
    serialise_broker_application,
    serialise_broker_session,
)

__all__ = [
    "BrokerApplication",
    "BrokerAuthState",
    "BrokerAuthenticator",
    "BrokerSession",
    "BrokerSessionMaterial",
    "SecretPrompt",
    "parse_broker_application",
    "parse_broker_session",
    "serialise_broker_application",
    "serialise_broker_session",
]
