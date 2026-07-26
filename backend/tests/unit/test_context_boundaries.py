"""The real import graph conforms to the approved context boundaries.

Two kinds of test live here:

1. **The live check** -- run the boundary checker over the actual source tree.
   This is the control that protects risk R13 ("the modular monolith degrades
   into a mud ball").
2. **Checker self-tests** -- synthetic trees that deliberately breach each rule,
   proving the checker would actually catch a breach. A green control that
   cannot fail is worse than no control, because it is trusted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dhruva.tooling.boundaries import (
    ALLOWED_CONTEXT_DEPENDENCIES,
    Violation,
    check_tree,
    find_repo_root,
)


def _write(src: Path, module: str, body: str) -> None:
    """Write ``body`` to the file corresponding to a dotted module name."""
    path = src.joinpath(*module.split(".")).with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _rules(violations: list[Violation]) -> set[str]:
    return {violation.rule for violation in violations}


# --------------------------------------------------------------------------- #
# The live check
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_real_source_tree_has_no_boundary_violations(source_root: Path, repo_root: Path) -> None:
    """The shipped codebase conforms to Master Project Plan section 5."""
    violations = check_tree(source_root, repo_root)
    assert not violations, "\n" + "\n".join(v.render() for v in violations)


@pytest.mark.unit
def test_dependency_matrix_is_acyclic() -> None:
    """A cycle in the declared matrix would make extraction to services impossible.

    This is the property that the Risk/Trading asymmetry exists to preserve
    (ADR-012): Trading imports Risk, Risk never imports Trading.
    """
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str, path: tuple[str, ...]) -> None:
        if node in done:
            return
        assert node not in visiting, f"dependency cycle: {' -> '.join([*path, node])}"
        visiting.add(node)
        for dependency in sorted(ALLOWED_CONTEXT_DEPENDENCIES[node]):
            visit(dependency, (*path, node))
        visiting.discard(node)
        done.add(node)

    for context in sorted(ALLOWED_CONTEXT_DEPENDENCIES):
        visit(context, ())


@pytest.mark.unit
def test_matrix_references_only_known_contexts() -> None:
    """A typo in the matrix would silently permit an undeclared dependency."""
    known = set(ALLOWED_CONTEXT_DEPENDENCIES)
    for context, dependencies in ALLOWED_CONTEXT_DEPENDENCIES.items():
        unknown = dependencies - known
        assert not unknown, f"context '{context}' declares unknown dependencies: {unknown}"
        assert context not in dependencies, f"context '{context}' declares itself"


@pytest.mark.unit
def test_risk_does_not_depend_on_trading() -> None:
    """ADR-012's asymmetry, asserted directly so a future edit cannot slip it in."""
    assert "trading" not in ALLOWED_CONTEXT_DEPENDENCIES["risk"]
    assert "risk" in ALLOWED_CONTEXT_DEPENDENCIES["trading"]


# --------------------------------------------------------------------------- #
# Checker self-tests
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_r1_detects_reaching_into_another_contexts_internals(synthetic_source: Path) -> None:
    """Importing a foreign context's domain layer is a violation, even if allowed by R2."""
    _write(
        synthetic_source,
        "dhruva.contexts.analytics.application.compute",
        "from dhruva.contexts.marketdata.domain.bar import Bar\n",
    )
    violations = check_tree(synthetic_source)
    assert "R1" in _rules(violations)
    assert "marketdata" in violations[0].message


@pytest.mark.unit
def test_r1_allows_import_through_the_public_api(synthetic_source: Path) -> None:
    """The sanctioned route is explicitly permitted."""
    _write(
        synthetic_source,
        "dhruva.contexts.analytics.application.compute",
        "from dhruva.contexts.marketdata.api import BarReader\n",
    )
    assert check_tree(synthetic_source) == []


@pytest.mark.unit
def test_r1_allows_from_package_import_api_form(synthetic_source: Path) -> None:
    """``from dhruva.contexts.x import api`` is the same sanctioned route."""
    _write(
        synthetic_source,
        "dhruva.contexts.analytics.application.compute",
        "from dhruva.contexts.marketdata import api\n",
    )
    assert check_tree(synthetic_source) == []


@pytest.mark.unit
def test_r2_detects_an_undeclared_dependency(synthetic_source: Path) -> None:
    """Reference is a root of the graph and may depend on no other context."""
    _write(
        synthetic_source,
        "dhruva.contexts.reference.application.instruments",
        "from dhruva.contexts.analytics.api import RegimeLabel\n",
    )
    violations = check_tree(synthetic_source)
    assert "R2" in _rules(violations)


@pytest.mark.unit
def test_r2_blocks_risk_importing_trading(synthetic_source: Path) -> None:
    """The ADR-012 asymmetry, enforced against a real import."""
    _write(
        synthetic_source,
        "dhruva.contexts.risk.application.authorise",
        "from dhruva.contexts.trading.api import Position\n",
    )
    violations = check_tree(synthetic_source)
    assert "R2" in _rules(violations)


@pytest.mark.unit
def test_r2_permits_trading_importing_risk(synthetic_source: Path) -> None:
    """The permitted direction stays permitted."""
    _write(
        synthetic_source,
        "dhruva.contexts.trading.application.place",
        "from dhruva.contexts.risk.api import RiskApprovedOrder\n",
    )
    assert check_tree(synthetic_source) == []


@pytest.mark.unit
def test_r3_detects_import_of_a_composition_root(synthetic_source: Path) -> None:
    """Nothing may depend inward on the wiring layer."""
    _write(
        synthetic_source,
        "dhruva.contexts.analytics.domain.regime",
        "from dhruva.api import settings\n",
    )
    violations = check_tree(synthetic_source)
    assert "R3" in _rules(violations)


@pytest.mark.unit
def test_r3_permits_composition_roots_importing_anything(synthetic_source: Path) -> None:
    """Composition roots are exempt: wiring is precisely their job."""
    _write(
        synthetic_source,
        "dhruva.api.main",
        "from dhruva.contexts.analytics.infrastructure.repo import Repo\n",
    )
    assert check_tree(synthetic_source) == []


@pytest.mark.unit
def test_r4_detects_the_shared_kernel_importing_a_context(synthetic_source: Path) -> None:
    """The shared kernel must stay a leaf, or every context becomes coupled."""
    _write(
        synthetic_source,
        "dhruva.shared.money",
        "from dhruva.contexts.reference.api import InstrumentId\n",
    )
    violations = check_tree(synthetic_source)
    assert "R4" in _rules(violations)


@pytest.mark.unit
def test_plain_import_statement_is_checked_too(synthetic_source: Path) -> None:
    """``import a.b.c`` is as capable of breaching a boundary as ``from`` is."""
    _write(
        synthetic_source,
        "dhruva.contexts.analytics.domain.regime",
        "import dhruva.contexts.trading.domain.order\n",
    )
    violations = check_tree(synthetic_source)
    assert {"R1", "R2"} <= _rules(violations)


@pytest.mark.unit
def test_non_dhruva_imports_are_ignored(synthetic_source: Path) -> None:
    """Third-party imports are not the checker's concern."""
    _write(
        synthetic_source,
        "dhruva.contexts.analytics.domain.regime",
        "import numpy\nfrom collections.abc import Sequence\n",
    )
    assert check_tree(synthetic_source) == []


@pytest.mark.unit
def test_violations_render_with_path_and_line(synthetic_source: Path) -> None:
    """Output is editor-parseable; a control nobody can act on is not a control."""
    _write(
        synthetic_source,
        "dhruva.contexts.reference.domain.thing",
        "\n\nfrom dhruva.contexts.trading.api import Order\n",
    )
    violations = check_tree(synthetic_source, synthetic_source)
    rendered = violations[0].render()
    assert rendered.startswith("dhruva/contexts/reference/domain/thing.py:3:")
    assert "[R2]" in rendered


@pytest.mark.unit
def test_find_repo_root_locates_the_marker() -> None:
    """Root resolution must not depend on the working directory."""
    root = find_repo_root(Path(__file__).parent)
    assert (root / ".dhruva-root").is_file()


@pytest.mark.unit
def test_find_repo_root_raises_when_no_marker_exists(tmp_path: Path) -> None:
    """Failing loudly beats silently checking the wrong tree."""
    with pytest.raises(FileNotFoundError, match=r"\.dhruva-root"):
        find_repo_root(tmp_path)
