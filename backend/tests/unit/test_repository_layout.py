"""The repository layout matches the approved architecture.

These are structural regression tests. They exist because the layout in Master
Project Plan section 13.1 is load-bearing: the boundary checker, the
import-linter contracts and the CI path filters all assume it. A rename that
silently breaks one of them would otherwise surface much later, as a control
that quietly stopped running.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from dhruva.tooling.boundaries import (
    ALLOWED_CONTEXT_DEPENDENCIES,
    COMPOSITION_ROOTS,
    LAYER_PACKAGES,
)

CONTEXTS = tuple(ALLOWED_CONTEXT_DEPENDENCIES)

#: Contexts that carry logic during Stage 1 (ADR-028). The rest exist as empty
#: contract-enforced boundaries until Gate G-MCP passes.
STAGE_ONE_CONTEXTS = frozenset({"reference", "marketdata", "analytics", "platform"})


@pytest.mark.unit
def test_nine_contexts_exist() -> None:
    """Plan section 5 declares exactly nine bounded contexts."""
    assert len(CONTEXTS) == 9


@pytest.mark.unit
@pytest.mark.parametrize("context", CONTEXTS)
def test_context_package_exists(source_root: Path, context: str) -> None:
    """Every declared context has a package with a docstring."""
    init = source_root / "dhruva" / "contexts" / context / "__init__.py"
    assert init.is_file(), f"missing package for context '{context}'"
    assert init.read_text(encoding="utf-8").lstrip().startswith('"""')


@pytest.mark.unit
@pytest.mark.parametrize("context", CONTEXTS)
def test_context_has_public_api_module(source_root: Path, context: str) -> None:
    """The ``api`` module is the only permitted import surface (boundary rule R1)."""
    api = source_root / "dhruva" / "contexts" / context / "api.py"
    assert api.is_file(), f"context '{context}' has no public api.py"
    assert "__all__" in api.read_text(encoding="utf-8")


@pytest.mark.unit
@pytest.mark.parametrize("context", CONTEXTS)
@pytest.mark.parametrize("layer", sorted(LAYER_PACKAGES))
def test_context_has_all_four_layers(source_root: Path, context: str, layer: str) -> None:
    """Clean Architecture layering is present in every context (plan section 3.2)."""
    init = source_root / "dhruva" / "contexts" / context / layer / "__init__.py"
    assert init.is_file(), f"context '{context}' is missing the '{layer}' layer"


@pytest.mark.unit
@pytest.mark.parametrize("root", sorted(COMPOSITION_ROOTS))
def test_composition_roots_exist(source_root: Path, root: str) -> None:
    """One package per process, per plan section 3.1."""
    assert (source_root / "dhruva" / root / "__init__.py").is_file()


@pytest.mark.unit
@pytest.mark.parametrize("context", CONTEXTS)
def test_context_api_module_imports_cleanly(context: str) -> None:
    """Every public API module must import without side effects and export nothing yet.

    Importing them here is not ceremony: it proves the nine public surfaces are
    real, importable modules rather than files that merely exist.
    """
    module = importlib.import_module(f"dhruva.contexts.{context}.api")
    assert module.__all__ == [], f"'{context}' exports names before its subsystem is built"


@pytest.mark.unit
def test_shared_kernel_exists(source_root: Path) -> None:
    """The shared kernel package is present, ready for S03."""
    assert (source_root / "dhruva" / "shared" / "__init__.py").is_file()


@pytest.mark.unit
def test_package_is_typed(source_root: Path) -> None:
    """PEP 561 marker so downstream type checking is not silently skipped."""
    assert (source_root / "dhruva" / "py.typed").is_file()


@pytest.mark.unit
def test_root_marker_is_present(repo_root: Path) -> None:
    """The marker makes every tool's root resolution deterministic."""
    assert (repo_root / ".dhruva-root").is_file()


@pytest.mark.unit
@pytest.mark.parametrize(
    "relative",
    [
        "backend/pyproject.toml",
        "backend/.importlinter",
        ".pre-commit-config.yaml",
        ".github/workflows/ci.yml",
        ".github/workflows/security.yml",
        "infra/docker-compose.yml",
        "docs/adr/README.md",
        "docs/adr/TEMPLATE.md",
        "docs/DHRUVA_MASTER_PROJECT_PLAN.md",
        "docs/DOMAIN.md",
        "docs/decisions.md",
        "docs/session-log.md",
        "Makefile",
        "README.md",
        "CONTRIBUTING.md",
        "CHANGELOG.md",
    ],
)
def test_required_repository_files_exist(repo_root: Path, relative: str) -> None:
    """Files that other controls depend on must not be renamed away silently."""
    assert (repo_root / relative).is_file(), f"missing required file: {relative}"


def _is_boundary_only(module: Path) -> bool:
    """Return whether a module contains nothing but a docstring and inert scaffolding.

    Permitted statements are the module docstring, ``from __future__`` imports,
    and an ``__all__`` assignment. Anything else -- a function, a class, an
    import of real machinery -- means the module has grown behaviour.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"))
    for index, node in enumerate(tree.body):
        if index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # module docstring
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            if all(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                continue
        return False
    return True


@pytest.mark.unit
def test_stage_two_contexts_carry_no_logic(source_root: Path) -> None:
    """ADR-028: contexts beyond Stage 1 exist as empty boundaries only.

    The moment one grows behaviour before Gate G-MCP, this test fails and forces
    the conversation. That is the intent: the gate is protected by a control, not
    by the author's memory of having agreed to it.
    """
    offenders = [
        str(module.relative_to(source_root))
        for context in CONTEXTS
        if context not in STAGE_ONE_CONTEXTS
        for module in sorted((source_root / "dhruva" / "contexts" / context).rglob("*.py"))
        if not _is_boundary_only(module)
    ]
    assert not offenders, (
        "Stage 2 contexts must contain no logic until Gate G-MCP passes (ADR-028). "
        f"Offending modules: {offenders}"
    )


@pytest.mark.unit
def test_no_order_placing_code_exists(source_root: Path) -> None:
    """ADR-028 and Gate G5: Stage 1 must contain nothing that can place an order.

    A crude textual scan is the right tool here precisely because it is crude --
    it catches the earliest, most innocent-looking version of the mistake, which
    is when catching it is cheapest.
    """
    forbidden = ("place_order", "kiteconnect", "KiteConnect", "order_variety", "transaction_type")
    offenders = [
        f"{module.relative_to(source_root)}: {token}"
        for module in sorted(source_root.rglob("*.py"))
        for token in forbidden
        if token in module.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "No order-constructing or broker-connecting code may exist during Stage 1 "
        f"(ADR-028). Found: {offenders}"
    )
