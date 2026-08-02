"""Refresh tokens, their lineage, and the rule that detects theft (ADR-072).

Pure domain. The interesting content is one function -- :meth:`RefreshToken.verdict`
-- and the reason it lives here rather than in the use case is that it is the
whole of ADR-072's reuse detection, and a security rule embedded in an
orchestration method is a security rule nobody can test in isolation.

The rule
--------
ADR-072:

    Presenting an already-used refresh token revokes its entire lineage and is
    recorded as an audited authentication event.

A token presented twice has one benign explanation -- a client retrying after a
lost response -- and one serious one, a stolen token being used alongside the
legitimate client. They are indistinguishable at the moment of presentation, so
ADR-022's fail-closed posture picks the serious reading: revoking the lineage
logs out an honest user occasionally and stops a thief every time.

Why the order of checks matters
-------------------------------
:meth:`verdict` tests revocation *before* reuse, and that is deliberate rather
than arbitrary. Detecting reuse revokes the whole lineage, which stamps
``revoked_at`` on the presented token too -- so a third presentation finds a
revoked token and returns :attr:`RefreshVerdict.REVOKED` rather than reporting
reuse again. That makes detection idempotent: an attacker hammering a stolen
token triggers one lineage revocation and one audit record, not one per request.

Why the token itself never appears here
---------------------------------------
The aggregate holds ``token_hash``, never the token. The secret exists in memory
for the duration of one response and is never stored, which is what makes a
stolen database a set of useless digests rather than a set of live sessions
(ADR-033).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from dhruva.shared.identity import AccountId, PrincipalId, RefreshTokenId

__all__ = ["RefreshToken", "RefreshVerdict"]


class RefreshVerdict(StrEnum):
    """What presenting a refresh token means.

    Four outcomes, and only one of them lets the session continue. They are
    distinct here because the *use case* must treat them differently -- reuse
    revokes a lineage and expiry does not -- while the **caller** is told far
    less: reuse and revocation both surface as the same error, because an error
    that distinguished them would tell a thief they had been detected.
    """

    USABLE = "usable"
    """Not revoked, not spent, not expired. Rotate it."""

    REVOKED = "revoked"
    """Explicitly revoked, or caught by a lineage revocation. No further
    action -- whatever prompted the revocation has already been recorded."""

    REUSED = "reused"
    """Already exchanged for a successor. The theft signal: revoke the lineage
    and audit it."""

    EXPIRED = "expired"
    """Past its lifetime. Ordinary, and not evidence of anything -- a session
    that ended while nobody was using it."""


@dataclass(frozen=True, slots=True)
class RefreshToken:
    """One issued refresh token and its place in a rotation chain.

    Attributes
    ----------
    token_id
        Domain identity. Safe to log; the token it represents is not.
    account_id
        The tenant (ADR-004).
    principal_id
        Whose session this is. Carried as well as ``account_id`` because "log
        this operator out" is unanswerable from a tenant alone.
    lineage_id
        Shared by every token in one rotation chain, so revoking a compromised
        family is one predicate rather than a recursive walk. The first token of
        a chain is its own lineage root, which the table enforces.
    token_hash
        Digest of the presented secret. Never the secret.
    issued_at, expires_at
        The token's fixed lifetime. ``expires_at`` is stored rather than derived
        so that changing the configured lifetime does not retroactively extend or
        shorten every outstanding session.
    parent_token_id
        The token this one replaced, or ``None`` for the first of a chain.
    replaced_by
        The token that replaced this one, or ``None`` while it is current. This
        is the field reuse detection reads.
    revoked_at
        When this token was revoked, or ``None``.
    """

    token_id: RefreshTokenId
    account_id: AccountId
    principal_id: PrincipalId
    lineage_id: UUID
    token_hash: bytes
    issued_at: datetime
    expires_at: datetime
    parent_token_id: RefreshTokenId | None = None
    replaced_by: RefreshTokenId | None = None
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        """Reject a token that could not be presented or checked."""
        invariant(bool(self.token_hash), "a refresh token must carry a hash")
        for name in ("issued_at", "expires_at"):
            value: datetime = getattr(self, name)
            invariant(
                value.tzinfo is not None and value.utcoffset() is not None,
                f"{name} must be timezone-aware",
                field=name,
                value=value.isoformat(),
            )
        invariant(
            self.expires_at > self.issued_at,
            "a refresh token must expire after it is issued",
            issued_at=self.issued_at.isoformat(),
            expires_at=self.expires_at.isoformat(),
        )
        # The first token of a chain anchors its own lineage. A successor
        # inherits the anchor; nothing invents a new one mid-chain, because a
        # chain with two lineage values is a chain revocation would half-miss.
        if self.parent_token_id is None:
            invariant(
                self.lineage_id == self.token_id.value,
                "the first token of a chain is its own lineage root",
                token_id=str(self.token_id),
                lineage_id=str(self.lineage_id),
            )

    @property
    def is_spent(self) -> bool:
        """Report whether this token has already been exchanged for a successor."""
        return self.replaced_by is not None

    @property
    def is_revoked(self) -> bool:
        """Report whether this token has been revoked."""
        return self.revoked_at is not None

    def is_expired(self, at: datetime) -> bool:
        """Report whether this token has passed its lifetime at ``at``.

        Evaluated against a supplied instant rather than the wall clock
        (ADR-011). Expiry is boundary-exclusive: a token is usable up to and
        including ``expires_at``, so a token minted and presented at the same
        instant in a test is not born expired.
        """
        return at > self.expires_at

    def verdict(self, at: datetime) -> RefreshVerdict:
        """Decide what presenting this token at ``at`` means.

        The order is load-bearing; see the module docstring. Revocation is
        checked first so that repeated presentation of a token whose lineage was
        already killed reports :attr:`RefreshVerdict.REVOKED` rather than
        detecting reuse over and over.

        Reuse is checked before expiry because a spent token presented after its
        expiry is still evidence that the chain leaked. Reporting it as merely
        expired would discard the signal at exactly the point somebody is using a
        stolen token slowly enough to avoid notice.
        """
        if self.is_revoked:
            return RefreshVerdict.REVOKED
        if self.is_spent:
            return RefreshVerdict.REUSED
        if self.is_expired(at):
            return RefreshVerdict.EXPIRED
        return RefreshVerdict.USABLE

    def succeeded_by(
        self,
        *,
        token_id: RefreshTokenId,
        token_hash: bytes,
        issued_at: datetime,
        expires_at: datetime,
    ) -> tuple[RefreshToken, RefreshToken]:
        """Rotate: return this token marked spent, and its successor.

        Returns
        -------
        tuple[RefreshToken, RefreshToken]
            The spent predecessor and the new token, in that order.

        Notes
        -----
        Both are returned because rotation is one atomic fact with two rows, and
        a method returning only the successor would let a caller write it while
        forgetting to close the predecessor -- which is precisely the state that
        makes a legitimately rotated token look reusable forever.

        The successor inherits ``lineage_id``, ``account_id`` and
        ``principal_id``. None of the three is a parameter, because a rotation
        that could change any of them would be a rotation that could move a
        session to another tenant.
        """
        successor = RefreshToken(
            token_id=token_id,
            account_id=self.account_id,
            principal_id=self.principal_id,
            lineage_id=self.lineage_id,
            token_hash=token_hash,
            issued_at=issued_at,
            expires_at=expires_at,
            parent_token_id=self.token_id,
        )
        return replace(self, replaced_by=token_id), successor

    def revoked(self, *, at: datetime) -> RefreshToken:
        """Return this token revoked at ``at``.

        Idempotent by intent: revoking an already-revoked token keeps the
        original instant, because the first revocation is the one that happened
        and overwriting it would move the evidence.
        """
        if self.is_revoked:
            return self
        return replace(self, revoked_at=at)
