"""Rotation, lineage, and the rule that calls a second presentation theft.

The verdict table is the whole of ADR-072's reuse detection, and it is pure
logic -- no database, no clock, no token. That is deliberate: a security rule
embedded in an orchestration method is a security rule nobody can enumerate the
cases of, and these are the cases.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID, uuid4

import pytest

from dhruva.contexts.platform.domain.identity import RefreshToken, RefreshVerdict
from dhruva.shared.errors import InvariantViolation
from dhruva.shared.identity import AccountId, PrincipalId, RefreshTokenId

pytestmark = pytest.mark.unit

ACCOUNT: Final = AccountId(UUID("11111111-1111-1111-1111-111111111111"))
PRINCIPAL: Final = PrincipalId(UUID("22222222-2222-2222-2222-222222222222"))
ISSUED: Final = datetime(2026, 8, 2, 9, 15, tzinfo=UTC)
EXPIRES: Final = ISSUED + timedelta(days=30)


def make_root(**overrides: object) -> RefreshToken:
    """Build the first token of a chain, which anchors its own lineage.

    The identity is always freshly minted and is deliberately not overridable.
    No test here needs to choose it, and a named ``token_id`` parameter would be
    the first thing ``**overrides`` binds against -- which is what made
    ``make_root(**{field: some_datetime})`` below a type error rather than a
    parametrised test.
    """
    identity = RefreshTokenId.new()
    fields: dict[str, object] = {
        "token_id": identity,
        "account_id": ACCOUNT,
        "principal_id": PRINCIPAL,
        "lineage_id": identity.value,
        "token_hash": b"digest-of-the-first-token",
        "issued_at": ISSUED,
        "expires_at": EXPIRES,
    }
    fields.update(overrides)
    return RefreshToken(**fields)  # type: ignore[arg-type]


def rotate(token: RefreshToken, *, at: datetime | None = None) -> tuple[RefreshToken, RefreshToken]:
    """Rotate a token, returning the spent predecessor and its successor."""
    moment = at or ISSUED + timedelta(minutes=5)
    return token.succeeded_by(
        token_id=RefreshTokenId.new(),
        token_hash=uuid4().bytes,
        issued_at=moment,
        expires_at=moment + timedelta(days=30),
    )


# --------------------------------------------------------------------------- #
# The verdict table. This is ADR-072's reuse detection.
# --------------------------------------------------------------------------- #


def test_a_fresh_token_is_usable() -> None:
    """The only verdict that lets a session continue."""
    assert make_root().verdict(ISSUED + timedelta(minutes=1)) is RefreshVerdict.USABLE


def test_a_spent_token_is_reuse() -> None:
    """The theft signal.

    One benign explanation -- a client retry after a lost response -- and one
    serious one. They are indistinguishable at presentation, so ADR-022's
    fail-closed posture takes the serious reading.
    """
    spent, _ = rotate(make_root())

    assert spent.verdict(ISSUED + timedelta(minutes=10)) is RefreshVerdict.REUSED


def test_an_expired_token_is_expired_and_not_evidence_of_anything() -> None:
    """A session that ended while nobody was using it."""
    assert make_root().verdict(EXPIRES + timedelta(seconds=1)) is RefreshVerdict.EXPIRED


def test_a_revoked_token_is_revoked() -> None:
    """Whatever prompted the revocation has already been recorded."""
    revoked = make_root().revoked(at=ISSUED + timedelta(hours=1))

    assert revoked.verdict(ISSUED + timedelta(hours=2)) is RefreshVerdict.REVOKED


def test_revocation_is_checked_before_reuse_so_detection_is_idempotent() -> None:
    """The ordering that stops an attacker generating one audit row per request.

    Detecting reuse revokes the lineage, which stamps ``revoked_at`` on the
    presented token too. A third presentation must therefore report REVOKED, not
    REUSED -- otherwise a stolen token hammered in a loop produces a lineage
    revocation and an audit record every time.
    """
    spent, _ = rotate(make_root())
    assert spent.verdict(ISSUED + timedelta(minutes=10)) is RefreshVerdict.REUSED

    after_lineage_revocation = spent.revoked(at=ISSUED + timedelta(minutes=10))

    assert after_lineage_revocation.verdict(ISSUED + timedelta(minutes=11)) is (
        RefreshVerdict.REVOKED
    )


def test_reuse_is_reported_even_after_the_token_would_have_expired() -> None:
    """A spent token presented late is still evidence the chain leaked.

    Reporting it as merely expired would discard the signal at exactly the point
    somebody is using a stolen token slowly enough to avoid notice.
    """
    spent, _ = rotate(make_root())

    assert spent.verdict(EXPIRES + timedelta(days=1)) is RefreshVerdict.REUSED


def test_expiry_is_inclusive_of_the_expiry_instant() -> None:
    """A token is usable up to and including ``expires_at``.

    Boundary-exclusive the other way would make a token minted and presented at
    the same instant in a test born expired, which is a test artefact rather
    than a policy.
    """
    token = make_root()

    assert token.is_expired(EXPIRES) is False
    assert token.is_expired(EXPIRES + timedelta(microseconds=1)) is True


# --------------------------------------------------------------------------- #
# Rotation
# --------------------------------------------------------------------------- #


def test_rotation_returns_both_halves_of_one_fact() -> None:
    """Two rows, one atomic act.

    A method returning only the successor would let a caller write it while
    forgetting to close the predecessor -- which is exactly the state that makes
    a legitimately rotated token look reusable forever.
    """
    original = make_root()
    spent, successor = rotate(original)

    assert spent.replaced_by == successor.token_id
    assert successor.parent_token_id == original.token_id


def test_a_successor_inherits_the_lineage() -> None:
    """One chain, one lineage value -- which is what makes revocation one query.

    A chain with two lineage values is a chain that revocation half-misses, and
    the half it misses is still usable.
    """
    root = make_root()
    _, successor = rotate(root)
    _, grandchild = rotate(successor)

    assert successor.lineage_id == root.lineage_id
    assert grandchild.lineage_id == root.lineage_id


def test_rotation_cannot_move_a_session_to_another_tenant_or_principal() -> None:
    """Neither is a parameter, so the mistake is unrepresentable rather than tested for."""
    _, successor = rotate(make_root())

    assert successor.account_id == ACCOUNT
    assert successor.principal_id == PRINCIPAL


def test_the_successor_is_usable_and_the_predecessor_is_not() -> None:
    """The point of rotation, stated as the pair of verdicts it produces."""
    spent, successor = rotate(make_root())
    later = ISSUED + timedelta(minutes=6)

    assert spent.verdict(later) is RefreshVerdict.REUSED
    assert successor.verdict(later) is RefreshVerdict.USABLE


def test_revoking_twice_keeps_the_first_instant() -> None:
    """The first revocation is the one that happened.

    Overwriting it would move the evidence, and the instant a lineage was killed
    is the thing an incident review reads.
    """
    first = ISSUED + timedelta(hours=1)
    once = make_root().revoked(at=first)

    assert once.revoked(at=first + timedelta(hours=1)).revoked_at == first


# --------------------------------------------------------------------------- #
# Invariants
# --------------------------------------------------------------------------- #


def test_the_first_token_of_a_chain_must_anchor_its_own_lineage() -> None:
    """Enforced here and by the table, because an orphan lineage revokes nothing."""
    with pytest.raises(InvariantViolation):
        make_root(lineage_id=uuid4())


def test_a_successor_may_carry_a_lineage_that_is_not_its_own_id() -> None:
    """The inverse of the rule above: only a root is constrained."""
    _, successor = rotate(make_root())

    assert successor.lineage_id != successor.token_id.value


def test_a_token_must_carry_a_hash() -> None:
    """An empty digest matches nothing, so the token could never be presented."""
    with pytest.raises(InvariantViolation):
        make_root(token_hash=b"")


def test_a_token_must_expire_after_it_is_issued() -> None:
    """A zero-length session is a configuration defect, not a session."""
    with pytest.raises(InvariantViolation):
        make_root(expires_at=ISSUED)


@pytest.mark.parametrize("field", ["issued_at", "expires_at"])
def test_both_timestamps_must_be_timezone_aware(field: str) -> None:
    """ADR-006, and an unordered token is one whose expiry cannot be decided."""
    with pytest.raises(InvariantViolation):
        make_root(**{field: datetime(2026, 8, 2, 9, 15)})  # noqa: DTZ001 - the point


def test_the_token_itself_is_never_a_field() -> None:
    """Structural: the aggregate holds a digest and has nowhere to put a secret.

    A stolen database must be a set of useless hashes rather than a set of live
    sessions (ADR-033). This fails if somebody adds a convenience field holding
    the presented value.
    """
    fields = set(RefreshToken.__dataclass_fields__)

    assert "token" not in fields
    assert "secret" not in fields
    assert "plaintext" not in fields
