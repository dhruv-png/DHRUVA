"""The environment enum is closed, and its predicates gate security branches."""

from __future__ import annotations

import pytest

from dhruva.shared.config import Environment


@pytest.mark.unit
def test_exactly_four_environments_exist() -> None:
    """Matches plan section 17. A fifth requires an ADR."""
    assert {e.value for e in Environment} == {"local", "test", "staging", "production"}


@pytest.mark.unit
def test_environment_compares_as_a_string() -> None:
    """StrEnum so configuration parsing and log rendering need no conversion.

    Asserted through ``isinstance`` and rendering rather than through direct
    equality against a literal: mypy's strict-equality check treats an enum
    member and a string literal as non-overlapping types, and silencing that
    would hide real comparison bugs elsewhere.
    """
    assert isinstance(Environment.PRODUCTION, str)
    assert Environment.PRODUCTION.value == "production"
    assert f"{Environment.LOCAL}" == "local"
    assert Environment("staging") is Environment.STAGING


@pytest.mark.unit
@pytest.mark.parametrize(
    ("environment", "is_development", "is_deployed", "allows_live_orders"),
    [
        (Environment.LOCAL, True, False, False),
        (Environment.TEST, True, False, False),
        (Environment.STAGING, False, True, False),
        (Environment.PRODUCTION, False, True, True),
    ],
)
def test_predicates(
    environment: Environment, *, is_development: bool, is_deployed: bool, allows_live_orders: bool
) -> None:
    """Each predicate gates a real branch, so each is pinned by table."""
    assert environment.is_development is is_development
    assert environment.is_deployed is is_deployed
    assert environment.allows_live_orders is allows_live_orders


@pytest.mark.unit
def test_staging_is_deployed_not_development() -> None:
    """A staging environment softer than production tests something else.

    Staging must reject development defaults and emit machine-readable logs
    exactly as production does, or it stops being a rehearsal.
    """
    assert Environment.STAGING.is_deployed
    assert not Environment.STAGING.is_development


@pytest.mark.unit
def test_only_production_could_ever_allow_live_orders() -> None:
    """ADR-026 and ADR-028. The predicate answers 'could it ever?', not 'now?'."""
    allowed = [e for e in Environment if e.allows_live_orders]

    assert allowed == [Environment.PRODUCTION]


@pytest.mark.unit
def test_an_unknown_environment_string_is_rejected() -> None:
    """A typo must fail loudly rather than silently selecting a soft default."""
    with pytest.raises(ValueError, match="uat"):
        Environment("uat")
