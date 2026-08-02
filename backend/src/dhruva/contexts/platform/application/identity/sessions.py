"""What a session is when it is handed back, and how one is prepared.

Shared by both use cases, because starting a session after a password and
continuing one after a rotation are nearly the same act: mint an access token,
mint a refresh token, and produce the row that will hold its digest. The
difference is one link -- a fresh login anchors its own lineage, a rotation
inherits one -- and keeping both here is what stops the two paths drifting into
different lifetimes.

Why preparation is pure
-----------------------
:func:`prepare_session` touches no repository and awaits nothing. It returns the
rows to be written and leaves the writing to the caller, which matters because
the two callers write differently: a login stages one row, a rotation stages one
*and* closes another, and that close is a conditional update whose result decides
whether the rotation happened at all. A helper that did the staging would have to
either hide that decision or return it awkwardly, and hiding it is how the loser
of a double-click would come to look like a thief.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from dhruva.contexts.platform.domain.identity.refresh import RefreshToken
from dhruva.contexts.platform.domain.identity.tokens import TokenClaims
from dhruva.shared.identity import RefreshTokenId

if TYPE_CHECKING:
    from datetime import datetime

    from dhruva.contexts.platform.domain.identity.ports import RefreshTokenMinter
    from dhruva.contexts.platform.domain.identity.principals import Principal
    from dhruva.contexts.platform.domain.identity.tokens import TokenIssuer
    from dhruva.shared.config.secret import SecretValue
    from dhruva.shared.identity import AccountId, PrincipalId

__all__ = ["IssuedSession", "PreparedSession", "SessionPolicy", "prepare_session"]


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A newly issued access and refresh token pair.

    The **only** moment the refresh token's secret exists anywhere. It is
    returned here, delivered to the client, and then forgotten -- the platform
    keeps its digest and nothing else.

    Attributes
    ----------
    access_token
        The signed JWT. Not a ``SecretValue``: a bearer token the client must put
        in a header cannot usefully be wrapped in a type whose purpose is to
        resist being turned into a string, and ADR-072 makes it short-lived and
        unrevocable by design rather than a stored credential.
    refresh_token
        A :class:`~dhruva.shared.config.secret.SecretValue`, because this one is
        long-lived and *is* a stored credential in every sense except that what
        is stored is its digest. Registered for redaction (ADR-037).
    access_expires_at
        When ``access_token`` stops verifying. Returned so a client can refresh
        before being refused rather than after.
    principal_id, account_id
        Who this session is for and which tenant it acts in.
    """

    access_token: str
    refresh_token: SecretValue
    access_expires_at: datetime
    principal_id: PrincipalId
    account_id: AccountId


@dataclass(frozen=True, slots=True)
class PreparedSession:
    """Everything a session needs written, decided but not yet staged.

    Attributes
    ----------
    session
        What the caller gets back once the writes succeed.
    refresh
        The row to insert.
    predecessor
        The row to close, when this continues an existing chain. ``None`` for a
        fresh login. Carried separately because closing it is a *conditional*
        update whose outcome decides whether the rotation happened -- see the
        module docstring.
    """

    session: IssuedSession
    refresh: RefreshToken
    predecessor: RefreshToken | None


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    """The two lifetimes, taken from configuration rather than invented.

    Passed in rather than read here, because an application module reaching for
    settings would be one that cannot be tested at another lifetime, and because
    plan §15.1's fifteen minutes should be visible at the composition root where
    somebody might change it.
    """

    access_token_seconds: int
    refresh_token_days: int


def prepare_session(
    principal: Principal,
    *,
    tokens: TokenIssuer,
    minter: RefreshTokenMinter,
    policy: SessionPolicy,
    now: datetime,
    predecessor: RefreshToken | None = None,
) -> PreparedSession:
    """Mint an access token and a refresh token, and build the rows to write.

    Parameters
    ----------
    predecessor
        Absent for a fresh login, which starts a new chain. Supplied for a
        rotation, which continues one: the successor inherits its lineage, which
        is the value revocation matches on and the reason a compromised family
        can be killed in one statement.

    Notes
    -----
    Nothing is written and nothing is awaited. The caller stages the returned
    rows inside its own transaction, so a session that could not be audited is a
    session that was never issued.
    """
    minted = minter.mint()
    claims = TokenClaims(
        subject=principal.subject,
        account_id=principal.account_id,
        expires_at=now + timedelta(seconds=policy.access_token_seconds),
    )
    expires_at = now + timedelta(days=policy.refresh_token_days)
    token_id = RefreshTokenId.new()

    if predecessor is None:
        spent = None
        refresh = RefreshToken(
            token_id=token_id,
            account_id=principal.account_id,
            principal_id=principal.principal_id,
            # A fresh login anchors its own lineage, which the table enforces.
            lineage_id=token_id.value,
            token_hash=minted.digest,
            issued_at=now,
            expires_at=expires_at,
        )
    else:
        # The domain builds both halves, so the predecessor cannot be left open
        # by a caller that forgot -- which is the state that makes a legitimately
        # rotated token look reusable forever.
        spent, refresh = predecessor.succeeded_by(
            token_id=token_id,
            token_hash=minted.digest,
            issued_at=now,
            expires_at=expires_at,
        )

    return PreparedSession(
        session=IssuedSession(
            access_token=tokens.mint(claims),
            refresh_token=minted.secret,
            access_expires_at=claims.expires_at,
            principal_id=principal.principal_id,
            account_id=principal.account_id,
        ),
        refresh=refresh,
        predecessor=spent,
    )
