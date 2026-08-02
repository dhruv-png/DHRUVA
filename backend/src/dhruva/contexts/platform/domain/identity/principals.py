"""The authenticating principal (ADR-072, ADR-073, ADR-004).

Pure domain. No database, no clock, no cipher -- this object holds a sealed TOTP
secret and has no way to open it, exactly as :class:`Credential` holds sealed
broker material and cannot open that.

Why a principal is not an account
---------------------------------
An :class:`~dhruva.shared.identity.AccountId` is a **tenant**: what ADR-004
scopes every row to. A :class:`~dhruva.shared.identity.PrincipalId` is **who
acted**: what an audit record names and what an access token asserts as its
subject. Today they are one-to-one, and that is precisely the danger -- a
codebase that conflates them while the mapping is one-to-one has no way to tell,
and finds out by discovering that a year of audit rows cannot say which human
did anything.

ADR-072 already separates them: :class:`TokenClaims` carries ``subject`` *and*
``account_id``. This module is where that separation becomes an object.

Why every change returns a new principal
----------------------------------------
Frozen, so ``enrol_totp`` and ``change_password`` return a new instance with an
incremented ``version`` rather than mutating in place. That is what makes
ADR-057's optimistic concurrency expressible: the repository writes
``WHERE version = :loaded`` and the aggregate it was handed already carries the
next value. A mutable aggregate would leave the repository guessing which version
it was supposed to have seen.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import datetime

    from dhruva.contexts.platform.domain.identity.credentials import EncryptedSecret
    from dhruva.contexts.platform.domain.identity.passwords import PasswordHash
    from dhruva.shared.identity import AccountId, PrincipalId

__all__ = ["Principal"]


@dataclass(frozen=True, slots=True)
class Principal:
    """One authenticating identity, with what proves it and what it belongs to.

    Attributes
    ----------
    principal_id
        Domain identity. Minted, never derived from ``subject`` -- an operator's
        login identifier is the value most likely to change, and deriving from it
        would re-identify the principal every time it did.
    account_id
        The tenant this principal acts within (ADR-004).
    subject
        The stable login identifier. This is the value that reaches
        ``TokenClaims.subject`` and ``AuditRecord.actor``, which is why it is
        carried on the principal rather than reconstructed at each call site.
    password_hash
        One-way. See :mod:`~...domain.identity.passwords` for why this is a type
        rather than a string.
    created_at, updated_at
        When the principal was first written and last written, both
        timezone-aware (ADR-006).
    totp_secret
        The sealed TOTP secret, or ``None`` when the principal has not enrolled.
        Sealed rather than hashed because a second factor is **shared** key
        material: the server recomputes the same codes the authenticator does, so
        it must be recoverable, which makes it exactly what ADR-070 exists for.

        This object cannot open it. Verification of a TOTP code is Step 7 and
        takes a ``KeyProvider`` explicitly, on the same argument that keeps the
        credential plaintext path to one named module.
    disabled_at
        When the principal was disabled, or ``None``. A disabled principal is
        refused at authentication; it is not deleted, because the audit trail
        references it and evidence that points at nothing is not evidence.
    version
        Optimistic-concurrency token (ADR-057).
    """

    principal_id: PrincipalId
    account_id: AccountId
    subject: str
    password_hash: PasswordHash
    created_at: datetime
    updated_at: datetime
    totp_secret: EncryptedSecret | None = None
    disabled_at: datetime | None = None
    version: int = 1

    def __post_init__(self) -> None:
        """Reject a principal that could not be identified or written back."""
        invariant(bool(self.subject.strip()), "a principal must have a subject")
        invariant(
            self.version >= 1,
            "version starts at 1",
            subject=self.subject,
            version=self.version,
        )
        for name in ("created_at", "updated_at"):
            value: datetime = getattr(self, name)
            invariant(
                value.tzinfo is not None and value.utcoffset() is not None,
                f"{name} must be timezone-aware",
                field=name,
                value=value.isoformat(),
            )
        invariant(
            self.updated_at >= self.created_at,
            "updated_at cannot precede created_at",
            created_at=self.created_at.isoformat(),
            updated_at=self.updated_at.isoformat(),
        )
        if self.disabled_at is not None:
            invariant(
                self.disabled_at.tzinfo is not None and self.disabled_at.utcoffset() is not None,
                "disabled_at must be timezone-aware",
                value=self.disabled_at.isoformat(),
            )

    @property
    def is_two_factor_enrolled(self) -> bool:
        """Report whether a second factor is enrolled.

        Derived from the sealed secret rather than stored beside it. A boolean
        column asserting the same fact is a column that can disagree with it,
        and because ADR-073 gates the order permission on enrolment, the two
        disagreeing means granting order placement to an account whose second
        factor does not work.
        """
        return self.totp_secret is not None

    @property
    def is_active(self) -> bool:
        """Report whether this principal may authenticate at all."""
        return self.disabled_at is None

    def with_password(self, password_hash: PasswordHash, *, at: datetime) -> Principal:
        """Return this principal with a new password, one version on.

        ``at`` is supplied rather than read from a clock (ADR-011). A domain
        object that called ``datetime.now()`` would be untestable without
        sleeping and wrong under replay.
        """
        return replace(
            self,
            password_hash=password_hash,
            updated_at=at,
            version=self.version + 1,
        )

    def enrolled(self, totp_secret: EncryptedSecret, *, at: datetime) -> Principal:
        """Return this principal with a second factor enrolled, one version on.

        Storage only, per this step's scope. What a valid TOTP secret *is*, how
        one is generated, and how a submitted code is checked against it are
        Step 7's, and none of them changes this signature.
        """
        return replace(self, totp_secret=totp_secret, updated_at=at, version=self.version + 1)

    def disabled(self, *, at: datetime) -> Principal:
        """Return this principal disabled, one version on.

        Disabling stops future authentication. It does **not** invalidate
        outstanding access tokens, which ADR-072 makes stateless and unrevocable
        for up to fifteen minutes; the caller that disables a principal should
        also revoke its refresh lineages, and the runbook says so because the
        gap is exactly where somebody expects immediacy.
        """
        return replace(self, disabled_at=at, updated_at=at, version=self.version + 1)
