"""Metric names, cardinality and secret-exclusion for S06 outcomes."""

from __future__ import annotations

import inspect

import pytest

from dhruva.contexts.platform.domain.identity import (
    AuthenticationOperation,
    AuthorisationOperation,
    SecurityOutcome,
)
from dhruva.contexts.platform.infrastructure.identity import (
    AUTHENTICATION_METRIC,
    AUTHORISATION_METRIC,
    PrometheusIdentityMetrics,
)
from dhruva.shared.observability import MetricsRegistry

pytestmark = pytest.mark.unit


def test_metric_names_and_labels_are_closed_and_bounded() -> None:
    """Two enums by one enum produce finite series; no caller text is accepted."""
    registry = MetricsRegistry()
    metrics = PrometheusIdentityMetrics(registry)

    for auth_operation in AuthenticationOperation:
        for outcome in SecurityOutcome:
            metrics.authentication(auth_operation, outcome)
    for authz_operation in AuthorisationOperation:
        for outcome in SecurityOutcome:
            metrics.authorisation(authz_operation, outcome)

    rendered = registry.render().decode("utf-8")
    assert registry.registered == (AUTHENTICATION_METRIC, AUTHORISATION_METRIC)
    assert rendered.count(f"{AUTHENTICATION_METRIC}{{") == 6
    assert rendered.count(f"{AUTHORISATION_METRIC}{{") == 6
    assert set(inspect.signature(metrics.authentication).parameters) == {"operation", "outcome"}
    assert set(inspect.signature(metrics.authorisation).parameters) == {"operation", "outcome"}


def test_metric_exposition_contains_no_identity_or_secret_material() -> None:
    """Subjects, credentials, hashes, tokens and TOTP material have no label path."""
    registry = MetricsRegistry()
    metrics = PrometheusIdentityMetrics(registry)
    metrics.authentication(AuthenticationOperation.LOGIN, SecurityOutcome.REFUSED)
    metrics.authentication(AuthenticationOperation.REFRESH, SecurityOutcome.ERROR)
    metrics.authorisation(AuthorisationOperation.GRANT, SecurityOutcome.REFUSED)

    rendered = registry.render().decode("utf-8")
    forbidden = {
        "operator@dhruva.local",
        "correct-horse-battery-staple",
        "refresh-token-DEADBEEF",
        "access-token-DEADBEEF",
        "argon2-password-hash",
        "sha256-refresh-hash",
        "totp-shared-secret",
        "manager-role",
        "place_order",
    }
    assert all(value not in rendered for value in forbidden)
