"""A credential must be unable to render itself.

The threat is not an attacker; it is an ordinary Tuesday. A settings object gets
logged, an exception carries a connection string, a traceback renders locals. In
each case the credential reaches a sink through a path no policy anticipated
(ADR-033). These tests close each of those paths.
"""

from __future__ import annotations

import copy
import json
import pickle

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dhruva.shared.config import REDACTED_PLACEHOLDER, SecretValue, registered_secret_values

#: A fake credential. Never registered anywhere real; its whole purpose is to
#: be searched for in rendered output.
SECRET = "kite-access-token-abcdef123456"  # noqa: S105 - fixture, not a real credential


@pytest.mark.unit
@pytest.mark.parametrize(
    ("description", "render"),
    [
        ("str()", str),
        ("repr()", repr),
        ("f-string", lambda s: f"{s}"),
        ("format()", format),
        ("str.format", "{}".format),
        ("%-formatting", lambda s: "%s" % (s,)),  # noqa: UP031 - the point is to test it
        ("concatenation via join", lambda s: " ".join(["prefix", str(s)])),
    ],
)
def test_no_rendering_path_reveals_the_value(description: str, render: object) -> None:
    """Every ordinary way of turning an object into text must be closed."""
    assert callable(render)
    output = render(SecretValue(SECRET))

    assert SECRET not in str(output), f"{description} leaked the credential"
    assert REDACTED_PLACEHOLDER in str(output)


@pytest.mark.unit
def test_format_spec_cannot_leak_a_prefix() -> None:
    """``f"{secret:.4}"`` must not become a truncation oracle."""
    assert f"{SecretValue(SECRET):.4}" == REDACTED_PLACEHOLDER


@pytest.mark.unit
def test_reveal_is_the_only_accessor() -> None:
    """Named so that every call site is greppable and reviewable."""
    assert SecretValue(SECRET).reveal() == SECRET


@pytest.mark.unit
def test_a_secret_cannot_be_pickled() -> None:
    """Pickling writes a credential to disk or onto a queue."""
    with pytest.raises(TypeError, match="cannot be pickled"):
        pickle.dumps(SecretValue(SECRET))


@pytest.mark.unit
def test_a_secret_can_be_copied_in_memory() -> None:
    """Copying must work, or a secret cannot be a validated default.

    Pickling is blocked because it writes the value somewhere; an in-memory copy
    never leaves the process, so restricting it would be cost without benefit.
    """
    original = SecretValue(SECRET)

    assert copy.copy(original) == original
    assert copy.deepcopy(original) == original
    assert str(copy.deepcopy(original)) == REDACTED_PLACEHOLDER


@pytest.mark.unit
def test_a_secret_cannot_be_hashed() -> None:
    """A hashable secret becomes a dict key, and dict keys end up in logs."""
    with pytest.raises(TypeError, match="not hashable"):
        hash(SecretValue(SECRET))


@pytest.mark.unit
def test_a_secret_is_not_json_serialisable_by_accident() -> None:
    """The default encoder must refuse rather than emit something plausible."""
    with pytest.raises(TypeError):
        json.dumps({"token": SecretValue(SECRET)})


@pytest.mark.unit
def test_equality_works_between_secrets() -> None:
    """Configuration is compared in tests; comparison must not require reveal()."""
    assert SecretValue(SECRET) == SecretValue(SECRET)
    assert SecretValue(SECRET) != SecretValue("something-else-entirely")


@pytest.mark.unit
def test_comparison_against_a_plain_string_is_not_supported() -> None:
    """``settings.token == "abc"`` should be a review smell, not a silent oracle."""
    assert SecretValue(SECRET).__eq__(SECRET) is NotImplemented
    assert SecretValue(SECRET) != SECRET


@pytest.mark.unit
def test_length_and_truthiness_are_disclosed_deliberately() -> None:
    """Validation needs both; neither is sensitive the way the value is."""
    assert len(SecretValue(SECRET)) == len(SECRET)
    assert SecretValue(SECRET)
    assert not SecretValue("", register=False)


@pytest.mark.unit
def test_secrets_join_the_redaction_registry() -> None:
    """The registry is what lets the log processor scrub interpolated values."""
    SecretValue("registry-probe-value-1234")

    assert "registry-probe-value-1234" in registered_secret_values()


@pytest.mark.unit
def test_short_values_are_not_registered() -> None:
    """Redacting the word 'test' out of every log line is worse than the leak.

    The floor exists to keep value-scanning from destroying ordinary log content.
    """
    SecretValue("abc", register=True)

    assert "abc" not in registered_secret_values()


@pytest.mark.unit
def test_registration_can_be_declined() -> None:
    """Test fixtures whose appearance in output is expected opt out."""
    SecretValue("fixture-value-not-registered", register=False)

    assert "fixture-value-not-registered" not in registered_secret_values()


@pytest.mark.unit
def test_the_registry_snapshot_is_immutable() -> None:
    """Callers must not be able to mutate the registry through the accessor."""
    assert isinstance(registered_secret_values(), frozenset)


@pytest.mark.unit
@given(value=st.text(min_size=1, max_size=200))
def test_no_generated_value_ever_renders(value: str) -> None:
    """Property test: non-disclosure holds for arbitrary content.

    Includes the cases a hand-written test misses -- unicode, whitespace-only
    strings, and values that happen to contain the placeholder itself.
    """
    secret = SecretValue(value, register=False)

    assert str(secret) == REDACTED_PLACEHOLDER
    assert repr(secret) == f"SecretValue({REDACTED_PLACEHOLDER})"
    assert secret.reveal() == value
