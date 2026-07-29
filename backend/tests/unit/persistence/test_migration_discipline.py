"""Every migration declares its reversibility and operational impact (ADR-055).

These are static checks over the migration files. They run without a database,
and they catch the failure that matters most at 09:20 on a trading day: a
migration nobody classified, so nobody knows whether it can be undone.

The up -> down -> up cycle itself requires PostgreSQL and lives in the
integration suite.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

#: Fields every migration docstring must carry (ADR-055, extended at Design Review).
REQUIRED_FIELDS = (
    "Reversibility:",
    "Rollback procedure:",
    "Irreversible operations:",
    "Expected runtime:",
    "Operational impact:",
)

#: The three permitted classifications. Anything else is an unreviewed answer.
VALID_CLASSIFICATIONS = ("reversible", "conditionally reversible", "irreversible")


def _migrations() -> list[Path]:
    versions = Path(__file__).resolve().parents[3] / "alembic" / "versions"
    return sorted(p for p in versions.glob("*.py") if not p.name.startswith("__"))


def _docstring(path: Path) -> str:
    return ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""


def test_at_least_one_migration_exists() -> None:
    """A guard on the guards: an empty directory would pass every check below."""
    assert _migrations(), "no migrations found; the checks below would be vacuous"


@pytest.mark.parametrize("migration", _migrations(), ids=lambda p: p.stem)
@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_every_migration_declares_the_required_fields(migration: Path, field: str) -> None:
    """Silence is not permitted.

    "No locks, sub-second, reversible" written down is *verified trivial*. The
    same migration with no header is *nobody checked*, and the two are
    indistinguishable when it matters.
    """
    assert field in _docstring(migration), f"{migration.name} does not declare {field}"


@pytest.mark.parametrize("migration", _migrations(), ids=lambda p: p.stem)
def test_every_migration_uses_a_valid_classification(migration: Path) -> None:
    """One of three answers, so the field cannot be filled in with prose."""
    match = re.search(r"Reversibility:\s*(.+)", _docstring(migration))

    assert match is not None
    assert match.group(1).strip().lower().startswith(VALID_CLASSIFICATIONS), (
        f"{migration.name} classification must be one of {VALID_CLASSIFICATIONS}"
    )


@pytest.mark.parametrize("migration", _migrations(), ids=lambda p: p.stem)
def test_no_migration_still_carries_the_template_placeholders(migration: Path) -> None:
    """An unedited template is worse than a missing one; it looks complete."""
    assert "REPLACE" not in _docstring(migration), (
        f"{migration.name} still contains template placeholders"
    )


@pytest.mark.parametrize("migration", _migrations(), ids=lambda p: p.stem)
def test_a_reversible_migration_implements_downgrade(migration: Path) -> None:
    """An empty downgrade silently claims a reversibility it does not have.

    A genuinely irreversible migration must *raise* with its reason, so an
    operator attempting rollback learns why rather than watching it succeed and
    do nothing.
    """
    tree = ast.parse(migration.read_text(encoding="utf-8"))
    downgrade = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "downgrade"
        ),
        None,
    )

    assert downgrade is not None, f"{migration.name} has no downgrade()"

    # Strip the leading docstring only. Filtering every ast.Expr would also
    # discard the op.drop_table() calls, which are expression statements -- and
    # would make this check pass vacuously for any migration that does work.
    body = list(downgrade.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    classification = _docstring(migration).lower()

    if "reversibility: irreversible" in classification:
        assert any(isinstance(node, ast.Raise) for node in body), (
            f"{migration.name} is irreversible and must raise with the reason"
        )
    else:
        assert body, f"{migration.name} claims reversibility but downgrade() is empty"
        assert not any(isinstance(node, ast.Pass) for node in body), (
            f"{migration.name} downgrade() is a no-op"
        )


def test_the_script_template_carries_the_required_header() -> None:
    """New migrations inherit the declaration, so it cannot be forgotten."""
    template = Path(__file__).resolve().parents[3] / "alembic" / "script.py.mako"
    content = template.read_text(encoding="utf-8")

    for field in REQUIRED_FIELDS:
        assert field in content, f"the migration template omits {field}"


def test_the_alembic_config_carries_no_database_url() -> None:
    """A credential must never land in a committed file (ADR-033).

    The URL is supplied at runtime from validated settings, so alembic.ini being
    empty here is the design rather than an omission.
    """
    config = Path(__file__).resolve().parents[3] / "alembic.ini"
    line = next(
        line
        for line in config.read_text(encoding="utf-8").splitlines()
        if line.startswith("sqlalchemy.url")
    )

    assert line.split("=", 1)[1].strip() == ""
