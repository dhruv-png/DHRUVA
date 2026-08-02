"""Deny by default, and order placement is never implied (ADR-073, plan §15.1).

Nothing here touches a database or a clock. The policy is a function of its
arguments, which is what lets the most important rule in the module -- that no
role can acquire order placement without being given it by name -- be asserted
exhaustively rather than by example.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from dhruva.contexts.platform.domain.identity import (
    TWO_FACTOR_REQUIRED,
    GrantRefusal,
    GrantVerdict,
    Permission,
    PermissionGrant,
    Role,
    is_permitted,
    may_grant,
)
from dhruva.shared.errors import InvariantViolation
from dhruva.shared.identity import AccountId

pytestmark = pytest.mark.unit

ACCOUNT = AccountId.deterministic("authorisation-policy")
CREATED = datetime(2026, 8, 2, 9, 15, tzinfo=UTC)


def _role(name: str, *permissions: Permission) -> Role:
    """Build a tenant-owned role while keeping policy tests about policy."""
    grants = frozenset(
        PermissionGrant(permission=permission, granted_at=CREATED, granted_by="security-admin")
        for permission in permissions
    )
    return Role(
        account_id=ACCOUNT,
        name=name,
        created_at=CREATED,
        updated_at=CREATED,
        grants=grants,
    )


# --------------------------------------------------------------------------- #
# Deny by default
# --------------------------------------------------------------------------- #


def test_a_role_with_nothing_granted_may_do_nothing() -> None:
    """The default, asserted rather than assumed. ADR-073: refused, not permitted."""
    empty = _role("observer")

    assert not any(is_permitted(empty, permission) for permission in Permission)


def test_a_permission_is_granted_only_by_being_named() -> None:
    """Membership is the whole mechanism."""
    trader = _role("trader", Permission.PLACE_ORDER)

    assert is_permitted(trader, Permission.PLACE_ORDER)


# --------------------------------------------------------------------------- #
# Order placement is never implied
# --------------------------------------------------------------------------- #


def test_a_role_granted_every_other_permission_still_cannot_place_an_order() -> None:
    """ADR-073's central guarantee, asserted exhaustively over the enumeration.

    Written this way on purpose: it stays true as permissions are added, and it
    is the test that fails if somebody introduces a wildcard or a hierarchy. The
    most expensive authorisation mistake this platform can make is an account
    that could place an order and was not meant to, and the realistic route to
    it is a plausible-looking "full access" grant rather than an obviously wrong
    one.
    """
    everything_else = _role("administrator", *(frozenset(Permission) - {Permission.PLACE_ORDER}))

    assert not is_permitted(everything_else, Permission.PLACE_ORDER)


def test_the_permission_set_is_exactly_what_the_accepted_design_names() -> None:
    """Pinned. Every member must have an accepted subsystem need.

    A permission set invented ahead of the subsystems that need it is
    architecture nobody decided. This fails when a member is added, which is the
    point: it should be added by the subsystem that needs it, in a visible diff.
    """
    assert sorted(permission.value for permission in Permission) == [
        "manage_authorisation",
        "place_order",
    ]


# --------------------------------------------------------------------------- #
# 2FA gates the grant, not the login
# --------------------------------------------------------------------------- #


def test_order_placement_cannot_be_granted_without_enrolled_totp() -> None:
    """Plan §15.1 makes TOTP mandatory for any account with order permissions."""
    verdict = may_grant(Permission.PLACE_ORDER, totp_enrolled=False)

    assert not verdict.permitted
    assert verdict.refusal is GrantRefusal.TWO_FACTOR_NOT_ENROLLED


def test_order_placement_may_be_granted_once_totp_is_enrolled() -> None:
    """The precondition is satisfiable, so the control blocks a gap rather than the feature."""
    assert may_grant(Permission.PLACE_ORDER, totp_enrolled=True).permitted


def test_every_two_factor_permission_is_refused_without_enrolment() -> None:
    """Over the set rather than the member, so a second one inherits the rule.

    ADR-073 places the check in the granting operation precisely so the
    invariant cannot be violated by omission. A test naming ``PLACE_ORDER``
    alone would not notice the omission it exists to prevent.
    """
    for permission in TWO_FACTOR_REQUIRED:
        assert not may_grant(permission, totp_enrolled=False).permitted


# --------------------------------------------------------------------------- #
# Value semantics
# --------------------------------------------------------------------------- #


def test_a_role_cannot_be_edited() -> None:
    """A role a caller can edit at runtime is a role two processes disagree about."""
    role = _role("trader")

    with pytest.raises(dataclasses.FrozenInstanceError):
        role.name = "administrator"  # type: ignore[misc]


def test_a_role_must_have_a_storable_name_and_version() -> None:
    """The domain refuses the two states the table constraints also reject."""
    with pytest.raises(InvariantViolation, match="must have a name"):
        Role(account_id=ACCOUNT, name=" \t", created_at=CREATED, updated_at=CREATED)

    with pytest.raises(InvariantViolation, match="version starts at 1"):
        Role(
            account_id=ACCOUNT,
            name="operator",
            created_at=CREATED,
            updated_at=CREATED,
            version=0,
        )


def test_a_live_permission_cannot_predate_its_role() -> None:
    """A grant timestamp is evidence only if the chronology is possible."""
    grant = PermissionGrant(
        permission=Permission.PLACE_ORDER,
        granted_at=datetime(2026, 8, 2, 9, 14, tzinfo=UTC),
        granted_by="security-admin",
    )

    with pytest.raises(InvariantViolation, match="cannot predate"):
        Role(
            account_id=ACCOUNT,
            name="operator",
            created_at=CREATED,
            updated_at=CREATED,
            grants=frozenset({grant}),
        )


def test_a_role_cannot_hold_two_live_rows_for_one_permission() -> None:
    """The aggregate mirrors the role_permission primary key rather than collapsing it."""
    grants = frozenset(
        {
            PermissionGrant(
                permission=Permission.PLACE_ORDER,
                granted_at=CREATED,
                granted_by="first-admin",
            ),
            PermissionGrant(
                permission=Permission.PLACE_ORDER,
                granted_at=CREATED,
                granted_by="second-admin",
            ),
        }
    )

    with pytest.raises(InvariantViolation, match="one live grant"):
        Role(
            account_id=ACCOUNT,
            name="operator",
            created_at=CREATED,
            updated_at=CREATED,
            grants=grants,
        )


def test_a_verdict_must_permit_or_refuse_but_never_both() -> None:
    """The same shape ``RequeueVerdict`` uses, so a caller reads one idiom, not two."""
    with pytest.raises(ValueError, match="never both or neither"):
        GrantVerdict(permitted=True, refusal=GrantRefusal.TWO_FACTOR_NOT_ENROLLED)

    with pytest.raises(ValueError, match="never both or neither"):
        GrantVerdict(permitted=False)
