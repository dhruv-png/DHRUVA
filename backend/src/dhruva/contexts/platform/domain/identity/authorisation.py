"""Who may do what (ADR-073, plan §15.1).

Pure policy. No database, no clock, no framework -- the whole module is a
function of its arguments, in the style of ``may_requeue`` next door in
``domain/messaging/dead_letters.py``.

Why there is no role hierarchy and no wildcard
----------------------------------------------
ADR-073 requires that order placement is **never implied** by an administrative
role, an "owner" role, or any notion of full access. A hierarchy is precisely a
mechanism for implying permissions, and a wildcard is one for granting them
without naming them. Neither exists here, so :func:`is_permitted` can only answer
yes to a permission that was written down -- and the guarantee is structural
rather than a rule somebody has to remember when adding the next role.

That is also why :data:`Permission` stays deliberately small. Plan §15.1 names
order placement, and S06.7 adds only the capability needed to administer
explicit grants. Members are added by the subsystem that needs them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from dhruva.shared.invariants import invariant

if TYPE_CHECKING:
    from datetime import datetime

    from dhruva.shared.identity import AccountId

__all__ = [
    "GrantRefusal",
    "GrantVerdict",
    "Permission",
    "PermissionGrant",
    "Role",
    "is_permitted",
    "may_grant",
]


class Permission(StrEnum):
    """A capability that must be granted by name.

    Deliberately small: every member has an accepted subsystem need.
    """

    PLACE_ORDER = "place_order"
    """Submit, modify or cancel an order. ADR-073's central case: the risk gate
    (ADR-012) makes order placement unbypassable, and this makes it unimplied."""

    MANAGE_AUTHORISATION = "manage_authorisation"
    """Grant or revoke an explicitly named permission on a tenant role."""


#: Permissions whose grant requires enrolled TOTP. This includes order authority
#: under plan §15.1 and permission-management authority under S06.7. A frozenset
#: keeps the policy explicit and exhaustively testable.
TWO_FACTOR_REQUIRED: frozenset[Permission] = frozenset(
    {Permission.PLACE_ORDER, Permission.MANAGE_AUTHORISATION}
)


@dataclass(frozen=True, slots=True)
class PermissionGrant:
    """One explicitly accountable permission held by a role.

    The audit log answers how authorisation changed over time. These fields
    answer the live-state question: who is responsible for the permission this
    role holds now, and when was it granted? Keeping them in the aggregate makes
    persistence round-trippable instead of discarding evidence at the domain
    boundary.
    """

    permission: Permission
    granted_at: datetime
    granted_by: str

    def __post_init__(self) -> None:
        """Reject an unattributed or temporally ambiguous grant."""
        invariant(bool(self.granted_by.strip()), "a permission grant must name its actor")
        invariant(
            self.granted_at.tzinfo is not None and self.granted_at.utcoffset() is not None,
            "granted_at must be timezone-aware",
            value=self.granted_at.isoformat(),
        )


@dataclass(frozen=True, slots=True)
class Role:
    """A tenant-scoped, named set of explicitly granted permissions.

    Frozen, so a later grant or revoke returns a new version rather than mutating
    shared state. The repository can therefore apply ADR-057's
    ``UPDATE ... WHERE version = :loaded`` rule without guessing which version
    its caller originally observed.
    """

    account_id: AccountId
    name: str
    created_at: datetime
    updated_at: datetime
    grants: frozenset[PermissionGrant] = field(default_factory=frozenset)
    version: int = 1

    def __post_init__(self) -> None:
        """Reject a role that cannot be tenant-scoped or safely written back."""
        invariant(bool(self.name.strip()), "a role must have a name")
        invariant(self.version >= 1, "version starts at 1", role=self.name, version=self.version)
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
        permissions = [grant.permission for grant in self.grants]
        invariant(
            len(permissions) == len(set(permissions)),
            "a role may carry only one live grant per permission",
            role=self.name,
        )
        for grant in self.grants:
            invariant(
                grant.granted_at >= self.created_at,
                "a permission cannot predate its role",
                role=self.name,
                permission=grant.permission.value,
            )
            invariant(
                self.updated_at >= grant.granted_at,
                "updated_at cannot precede a live permission grant",
                role=self.name,
                permission=grant.permission.value,
            )

    @property
    def granted(self) -> frozenset[Permission]:
        """Return the permissions granted by name, without their evidence fields."""
        return frozenset(grant.permission for grant in self.grants)


def is_permitted(role: Role, required: Permission) -> bool:
    """Return whether ``role`` may perform ``required``.

    Deny by default: membership is the only thing that grants. There is no
    hierarchy to inherit through and no wildcard to match, so a permission that
    was never written down cannot be answered yes.

    Parameters
    ----------
    role
        The role the caller holds.
    required
        The permission the operation declares. ADR-073 refuses an operation that
        declares none, and that refusal belongs at the interfaces layer where
        routes are registered -- it cannot be expressed here, because "no
        permission" is not a value this function can be passed.
    """
    return required in role.granted


class GrantRefusal(StrEnum):
    """Why a grant was refused. Named, so an operator is told rather than guessing."""

    TWO_FACTOR_NOT_ENROLLED = "two_factor_not_enrolled"
    """The permission requires TOTP and the account has none. Plan §15.1 makes
    2FA mandatory for any account with order permissions, and ADR-073 places the
    check in the granting operation so the invariant cannot be violated by
    omission -- only by deliberately changing this rule."""


@dataclass(frozen=True, slots=True)
class GrantVerdict:
    """Whether a permission may be granted, and why not if it may not."""

    permitted: bool
    refusal: GrantRefusal | None = None

    def __post_init__(self) -> None:
        """Refuse a verdict that says both or neither."""
        if self.permitted is (self.refusal is not None):
            message = "a verdict must either permit or name a refusal, never both or neither"
            raise ValueError(message)


#: The one permitted verdict, shared rather than reconstructed per call.
PERMITTED = GrantVerdict(permitted=True)


def may_grant(permission: Permission, *, totp_enrolled: bool) -> GrantVerdict:
    """Decide whether ``permission`` may be granted to an account.

    Parameters
    ----------
    permission
        The permission being granted.
    totp_enrolled
        Whether the receiving account has TOTP enrolled.

    Notes
    -----
    Expressed as a precondition of the *grant* rather than as a property of the
    user, per ADR-073. The alternative -- enrol 2FA, then separately remember to
    require it -- is correct until the first hurried afternoon, and it fails
    open.
    """
    if permission in TWO_FACTOR_REQUIRED and not totp_enrolled:
        return GrantVerdict(permitted=False, refusal=GrantRefusal.TWO_FACTOR_NOT_ENROLLED)
    return PERMITTED
