"""Architectural boundary checker.

Enforces, against the real import graph, the bounded-context rules declared in
Master Project Plan section 5 and ADR-001.

Four rules are checked:

R1  **Public API only.** A context may reach another context only through that
    context's ``api`` module. Reaching into ``domain``, ``application``,
    ``infrastructure`` or ``interfaces`` is a violation.
R2  **Declared dependencies only.** A context may depend only on the contexts
    listed for it in :data:`ALLOWED_CONTEXT_DEPENDENCIES`, which is a direct
    transcription of the plan's section 5 table.
R3  **No inward dependency on composition roots.** ``dhruva.api``,
    ``dhruva.workers`` and ``dhruva.ingest`` wire the application together; they
    are imported by nothing.
R4  **Shared kernel and tooling stay leaf packages.** ``dhruva.shared`` and
    ``dhruva.tooling`` import nothing from ``dhruva.contexts``.
R5  **Only the configuration module reads the environment.** Nothing outside
    ``dhruva.shared.config`` may touch ``os.environ``, ``os.getenv`` or
    ``dotenv`` (ADR-031).

Why this exists as code rather than as a convention
---------------------------------------------------
R13 in the plan's risk register is "the modular monolith degrades into a mud
ball despite intent". Intent is not a control. This module is the control, and
it runs on every commit.

Why not import-linter alone
---------------------------
import-linter enforces the *layer* ordering inside each context, which it does
well and which this module deliberately does not duplicate. It cannot express
"context A may reach context B, but only through ``B.api``" without a
combinatorial explosion of hand-written contracts. That rule is the one most
likely to be violated in practice, so it gets a purpose-built checker whose
policy is a single readable dictionary.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ALLOWED_CONTEXT_DEPENDENCIES",
    "COMPOSITION_ROOTS",
    "CONFIG_PACKAGE",
    "LAYER_PACKAGES",
    "ROOT_MARKER",
    "Violation",
    "check_tree",
    "find_repo_root",
    "main",
]

#: Transcription of Master Project Plan section 5, "Bounded Contexts".
#: The mapping is the single source of truth for permitted context-to-context
#: dependencies. Changing it requires an ADR (ADR-027).
#:
#: Note the asymmetry between ``risk`` and ``trading``: Trading imports Risk
#: (it needs the ``RiskApprovedOrder`` token, ADR-012), while Risk never imports
#: Trading -- it reads positions through a ``PositionReader`` port that Risk
#: itself declares and Trading's infrastructure implements. That asymmetry is
#: what keeps this graph acyclic.
ALLOWED_CONTEXT_DEPENDENCIES: dict[str, frozenset[str]] = {
    "reference": frozenset(),
    "marketdata": frozenset({"reference"}),
    "analytics": frozenset({"reference", "marketdata"}),
    "intelligence": frozenset({"reference", "marketdata", "analytics"}),
    "strategy": frozenset({"reference", "marketdata", "analytics", "intelligence", "risk"}),
    "risk": frozenset({"reference", "marketdata"}),
    "trading": frozenset({"reference", "risk"}),
    "notification": frozenset(
        {"reference", "marketdata", "analytics", "intelligence", "strategy", "risk", "trading"}
    ),
    "platform": frozenset(),
}

#: Packages that wire adapters to ports. They may import anything; nothing may
#: import them.
COMPOSITION_ROOTS: frozenset[str] = frozenset({"api", "workers", "ingest"})

#: The Clean Architecture layers inside every context (ADR-001, plan section 3.2).
LAYER_PACKAGES: frozenset[str] = frozenset(
    {"domain", "application", "infrastructure", "interfaces"}
)

#: Marker file identifying the repository root unambiguously, so the checker
#: behaves identically from any working directory.
ROOT_MARKER = ".dhruva-root"

#: The only package permitted to read the environment (ADR-031, rule R5).
CONFIG_PACKAGE = "dhruva.shared.config"

#: Names that constitute reading the environment directly.
_ENVIRONMENT_ACCESSORS: frozenset[str] = frozenset({"environ", "getenv", "putenv", "environb"})

_PUBLIC_API_MODULE = "api"

# Segment counts in a dotted module name, used to classify a module by position:
#   dhruva . contexts . <context> . <layer> . <module>
#      1         2          3          4         5
_CONTEXT_SEGMENTS = 3
_LAYER_SEGMENTS = 4
_COMPOSITION_ROOT_SEGMENTS = 2
_LEAF_PACKAGE_PREFIXES = ("dhruva.shared", "dhruva.tooling")


@dataclass(frozen=True, slots=True)
class Violation:
    """A single architectural boundary breach.

    Attributes
    ----------
    path
        File containing the offending import, relative to the repository root.
    lineno
        Line number of the offending import.
    rule
        Short rule identifier, one of ``R1``..``R5``.
    message
        Human-readable explanation, written to be actionable without needing to
        consult the plan.
    """

    path: str
    lineno: int
    rule: str
    message: str

    def render(self) -> str:
        """Return a compact, editor-parseable representation of the violation."""
        return f"{self.path}:{self.lineno}: [{self.rule}] {self.message}"


def find_repo_root(start: Path | None = None) -> Path:
    """Locate the repository root by walking upward to the marker file.

    Parameters
    ----------
    start
        Directory to begin the search from. Defaults to the current directory.

    Returns
    -------
    Path
        The directory containing :data:`ROOT_MARKER`.

    Raises
    ------
    FileNotFoundError
        If no marker is found in ``start`` or any of its ancestors.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ROOT_MARKER).is_file():
            return candidate
    msg = f"no {ROOT_MARKER} marker found in {current} or any parent directory"
    raise FileNotFoundError(msg)


def _environment_reads(tree: ast.AST) -> list[tuple[str, int]]:
    """Find every direct read of the process environment in ``tree``.

    Catches all the spellings that matter: ``os.environ[...]``,
    ``os.getenv(...)``, ``from os import environ``, and any use of ``dotenv``.
    Returns ``(description, lineno)`` pairs.

    Notes
    -----
    This is intentionally syntactic. A determined caller could reach the
    environment through ``importlib`` or ``getattr``, and no AST rule will see
    that -- but such indirection is itself a review smell, and the rule exists to
    stop the ordinary, well-meaning case: someone adding ``os.getenv("DEBUG")``
    to a module at 6pm because threading a setting through felt like too much
    work.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(
                (alias.name, node.lineno)
                for alias in node.names
                if alias.name.split(".")[0] == "dotenv"
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] == "dotenv":
                found.append((f"dotenv.{node.names[0].name}", node.lineno))
            elif module == "os":
                found.extend(
                    (f"os.{alias.name}", node.lineno)
                    for alias in node.names
                    if alias.name in _ENVIRONMENT_ACCESSORS
                )
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr in _ENVIRONMENT_ACCESSORS
        ):
            found.append((f"os.{node.attr}", node.lineno))
    return found


def _module_name(source_root: Path, file: Path) -> str:
    """Convert a file path under ``source_root`` into a dotted module name."""
    relative = file.relative_to(source_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_relative(package: str, module: str, level: int) -> str:
    """Resolve a relative import to an absolute dotted module name.

    ``level`` is the number of leading dots. One dot means the containing
    package, two means its parent, and so on -- so ``level - 1`` segments are
    dropped from ``package`` before ``module`` is appended.

    Relative imports are banned by ruff's ``TID`` rules, but the checker resolves
    them anyway: a boundary rule that can be evaded by changing import style is
    not a rule.
    """
    parts = package.split(".")
    keep = len(parts) - (level - 1)
    prefix = ".".join(parts[:keep]) if keep > 0 else ""
    if not prefix:
        return module
    return f"{prefix}.{module}" if module else prefix


def _imported_modules(tree: ast.AST, package: str) -> Iterator[tuple[str, int]]:
    """Yield ``(imported_module, lineno)`` for every ``dhruva.*`` import.

    ``from a.b import c`` is ambiguous: ``c`` may be a submodule or a name defined
    inside ``a.b``. Both interpretations are yielded so the most specific one can
    be matched by the rules. This errs towards precision rather than towards
    silently missing a boundary breach.

    Parameters
    ----------
    tree
        Parsed module.
    package
        The package *containing* the module, used to resolve relative imports.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("dhruva"):
                    yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = _resolve_relative(package, base, node.level)
            if not base.startswith("dhruva"):
                continue
            yield base, node.lineno
            for alias in node.names:
                if alias.name != "*":
                    yield f"{base}.{alias.name}", node.lineno


def _owning_context(module: str) -> str | None:
    """Return the context a module belongs to, or ``None`` if it is not in one."""
    parts = module.split(".")
    if len(parts) >= _CONTEXT_SEGMENTS and parts[0] == "dhruva" and parts[1] == "contexts":
        return parts[2]
    return None


def _is_composition_root(module: str) -> bool:
    """Return whether ``module`` lives inside a composition root package."""
    parts = module.split(".")
    return (
        len(parts) >= _COMPOSITION_ROOT_SEGMENTS
        and parts[0] == "dhruva"
        and parts[1] in COMPOSITION_ROOTS
    )


def _reaches_context_internals(module: str) -> bool:
    """Return whether ``module`` addresses a layer package inside a context."""
    parts = module.split(".")
    return len(parts) >= _LAYER_SEGMENTS and parts[3] in LAYER_PACKAGES


def _touches_public_api(module: str) -> bool:
    """Return whether ``module`` addresses a context's public ``api`` module."""
    parts = module.split(".")
    return len(parts) >= _LAYER_SEGMENTS and parts[3] == _PUBLIC_API_MODULE


def _check_matrix(
    source_context: str, target_context: str, path: str, lineno: int
) -> list[Violation]:
    """Apply rule R2: the dependency must appear in the declared matrix."""
    allowed = ALLOWED_CONTEXT_DEPENDENCIES.get(source_context, frozenset())
    if target_context in allowed:
        return []
    permitted = ", ".join(sorted(allowed)) or "no other context"
    return [
        Violation(
            path=path,
            lineno=lineno,
            rule="R2",
            message=(
                f"context '{source_context}' may not depend on '{target_context}'. "
                f"Declared dependencies: {permitted}. Changing this requires an ADR "
                f"(ADR-027) and an update to ALLOWED_CONTEXT_DEPENDENCIES."
            ),
        )
    ]


def _check_context_import(
    source_context: str, targets: Sequence[str], path: str, lineno: int
) -> list[Violation]:
    """Apply rules R1 and R2 to one import statement inside a context."""
    foreign: dict[str, str] = {}
    for target in targets:
        owner = _owning_context(target)
        if owner is not None and owner != source_context:
            foreign[target] = owner
    if not foreign:
        return []

    # `from dhruva.contexts.x import api` yields both `...x` and `...x.api`;
    # the import is legitimate if any interpretation lands on the public API.
    if any(_touches_public_api(target) for target in foreign):
        target_context = next(iter(foreign.values()))
        return _check_matrix(source_context, target_context, path, lineno)

    violations: list[Violation] = []
    for target, target_context in foreign.items():
        if _reaches_context_internals(target):
            violations.append(
                Violation(
                    path=path,
                    lineno=lineno,
                    rule="R1",
                    message=(
                        f"context '{source_context}' imports '{target}', reaching into "
                        f"'{target_context}' internals. Import "
                        f"'dhruva.contexts.{target_context}.api' instead, and export what "
                        f"you need from there."
                    ),
                )
            )
        violations.extend(_check_matrix(source_context, target_context, path, lineno))
    return _deduplicate(violations)


def _deduplicate(violations: list[Violation]) -> list[Violation]:
    """Collapse identical violations produced by the ambiguous-import expansion."""
    seen: set[Violation] = set()
    unique: list[Violation] = []
    for violation in violations:
        if violation not in seen:
            seen.add(violation)
            unique.append(violation)
    return unique


def _check_module(source_root: Path, file: Path, repo_root: Path) -> list[Violation]:
    """Check every ``dhruva`` import in a single file against all four rules."""
    module_name = _module_name(source_root, file)
    try:
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
    except SyntaxError as exc:  # pragma: no cover - a syntax error fails ruff first
        msg = f"{file}: could not be parsed: {exc}"
        raise ValueError(msg) from exc

    path = file.relative_to(repo_root).as_posix()
    # For a package's ``__init__``, the containing package is the module itself.
    package = (
        module_name
        if file.name == "__init__.py"
        else module_name.rsplit(".", 1)[0]
        if "." in module_name
        else module_name
    )
    source_context = _owning_context(module_name)
    in_composition_root = _is_composition_root(module_name)
    is_leaf_package = module_name.startswith(_LEAF_PACKAGE_PREFIXES)

    if not module_name.startswith(CONFIG_PACKAGE):
        violations_r5 = [
            Violation(
                path=path,
                lineno=lineno,
                rule="R5",
                message=(
                    f"'{module_name}' reads the environment via '{accessor}'. Only "
                    f"'{CONFIG_PACKAGE}' may do so (ADR-031). Add the value to the "
                    f"settings schema and have it injected."
                ),
            )
            for accessor, lineno in _environment_reads(tree)
        ]
    else:
        violations_r5 = []

    grouped: dict[int, list[str]] = {}
    for target, lineno in _imported_modules(tree, package):
        grouped.setdefault(lineno, []).append(target)

    violations: list[Violation] = list(violations_r5)
    for lineno, targets in sorted(grouped.items()):
        if not in_composition_root:
            roots = [t for t in targets if _is_composition_root(t)]
            if roots:
                violations.append(
                    Violation(
                        path=path,
                        lineno=lineno,
                        rule="R3",
                        message=(
                            f"'{module_name}' imports the composition root '{roots[0]}'. "
                            f"Composition roots wire the application together and are "
                            f"imported by nothing; invert the dependency with a port."
                        ),
                    )
                )
        if is_leaf_package:
            contextual = [t for t in targets if _owning_context(t) is not None]
            if contextual:
                violations.append(
                    Violation(
                        path=path,
                        lineno=lineno,
                        rule="R4",
                        message=(
                            f"'{module_name}' imports '{contextual[0]}'. The shared kernel "
                            f"and the tooling package are leaves: they may be imported by "
                            f"contexts but must never import one."
                        ),
                    )
                )
        if source_context is not None:
            violations.extend(_check_context_import(source_context, targets, path, lineno))
    return violations


def check_tree(source_root: Path, repo_root: Path | None = None) -> list[Violation]:
    """Check every Python module under ``source_root``.

    Parameters
    ----------
    source_root
        Directory containing the ``dhruva`` package, i.e. ``backend/src``.
    repo_root
        Root used to render relative paths in messages. Defaults to
        ``source_root``.

    Returns
    -------
    list[Violation]
        Violations sorted by path, then line, then rule. Empty means the import
        graph conforms to plan section 5.
    """
    base = repo_root or source_root
    violations: list[Violation] = []
    for file in sorted(source_root.rglob("*.py")):
        violations.extend(_check_module(source_root, file, base))
    return sorted(violations, key=lambda v: (v.path, v.lineno, v.rule))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the boundary check as a command-line tool.

    Returns
    -------
    int
        ``0`` if the import graph conforms, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(
        prog="dhruva-check-boundaries",
        description="Enforce bounded-context boundaries (Master Project Plan section 5).",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=None,
        help="directory containing the dhruva package (default: <repo>/backend/src)",
    )
    args = parser.parse_args(argv)

    repo_root = find_repo_root()
    source_root: Path = args.source_root or repo_root / "backend" / "src"
    violations = check_tree(source_root, repo_root)

    if not violations:
        sys.stdout.write("boundaries: OK - import graph conforms to plan section 5\n")
        return 0

    sys.stdout.write(f"boundaries: {len(violations)} violation(s)\n\n")
    for violation in violations:
        sys.stdout.write(f"{violation.render()}\n")
    sys.stdout.write("\nSee docs/adr/ADR-001-modular-monolith-not-microservices.md\n")
    return 1


if __name__ == "__main__":  # pragma: no cover - exercised via the console script
    raise SystemExit(main())
