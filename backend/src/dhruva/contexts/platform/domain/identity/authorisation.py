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

That is also why :data:`Permission` has exactly one member. Plan §15.1 names
order placement and nothing else; inventing a permission set would be inventing
architecture. Members are added by the subsystem that needs them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "GrantRefusal",
    "GrantVerdict",
    "Permission",
    "Role",
    "is_permitted",
    "may_grant",
]


class Permission(StrEnum):
    """A capability that must be granted by name.

    One member, deliberately. Plan §15.1 names order placement as "a separately
    granted permission" and names no others.
    """

    PLACE_ORDER = "place_order"
    """Submit, modify or cancel an order. ADR-073's central case: the risk gate
    (ADR-012) makes order placement unbypassable, and this makes it unimplied."""


#: Permissions whose grant requires enrolled TOTP (plan §15.1: "TOTP 2FA
#: mandatory for any account with order permissions"). A frozenset rather than a
#: check against a single member, so a second order-bearing permission inherits
#: the requirement by being added here rather than by someone remembering.
TWO_FACTOR_REQUIRED: frozenset[Permission] = frozenset({Permission.PLACE_ORDER})


@dataclass(frozen=True, slots=True)
class Role:
    """A named set of explicitly granted permissions.

    Frozen, because a role a caller can edit at runtime is a role two processes
    disagree about -- the same reasoning ``JobDefinition`` carries in the worker
    registry.
    """

    name: str
    granted: frozenset[Permission] = field(default_factory=frozenset)


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
