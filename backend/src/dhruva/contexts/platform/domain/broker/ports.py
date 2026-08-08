"""Ports the broker-authentication use cases depend on.

Three, and each exists because the thing behind it is either vendor-specific or
untestable in place: the broker's HTTP handshake, the terminal a secret is typed
into, and -- added here rather than in ``identity.ports`` -- the ability to open
a sealed credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime

    from dhruva.contexts.platform.domain.broker.session import BrokerApplication
    from dhruva.shared.config.secret import SecretValue

__all__ = [
    "BrokerAuthenticator",
    "BrokerSessionMaterial",
    "SecretPrompt",
]


@dataclass(frozen=True, slots=True)
class BrokerSessionMaterial:
    """What a broker hands back when a request token is exchanged.

    Narrow on purpose. Kite's response carries fifteen fields including the
    user's email, avatar URL, enabled exchanges and permitted order types; this
    slice stores none of them. What is here is what the market-data adapters
    actually need to sign a request, plus enough to say when the session dies.

    Keeping the rest out is the decision, not an omission. A field stored
    "because it was in the response" is a field somebody later reads as
    authoritative, and the enabled-exchange list in particular is exactly the
    sort of stale copy that makes a permission bug hard to see.
    """

    token: SecretValue
    broker_user_id: str
    issued_at: datetime
    expires_at: datetime


@runtime_checkable
class SecretPrompt(Protocol):
    """Collects a secret from the operator without echoing it.

    A port because the real implementation reads a terminal, and a use case that
    called ``getpass`` directly could not be tested without one -- so the
    hidden-input requirement would end up asserted by reading the source rather
    than by running it.
    """

    def __call__(self, prompt: str) -> SecretValue:
        """Return what the operator typed, without having displayed it.

        Raises
        ------
        UnsafeConfigurationError
            If the input channel cannot hide what is typed. Failing is correct:
            a fallback to visible input would put an API secret into a terminal
            scrollback and, on Windows, into the console history buffer.
        """
        ...


@runtime_checkable
class BrokerAuthenticator(Protocol):
    """The broker's side of the login handshake.

    Two operations, deliberately no more. This port cannot place an order,
    cannot read a portfolio and cannot fetch a candle, because it is the object
    a credential-holding use case is handed and the smallest surface is the one
    that needs the least trust.
    """

    def login_url(self, application: BrokerApplication) -> str:
        """Return the broker's own login page for this application.

        A string rather than an opened browser: the operator authenticates in a
        browser they already trust, and a command that launched one would be
        making a decision about the operator's machine that it has no business
        making.
        """
        ...

    async def exchange(
        self,
        application: BrokerApplication,
        request_token: SecretValue,
    ) -> BrokerSessionMaterial:
        """Exchange a one-time request token for session material.

        Raises
        ------
        UpstreamAuthenticationError
            If the broker rejected the token or the signature.
        UpstreamTimeoutError, UpstreamUnavailableError, RateLimitedError
            For transport conditions the operator should retry rather than fix.
        DataQualityError
            If the broker's answer was accepted but could not be understood.
        """
        ...
