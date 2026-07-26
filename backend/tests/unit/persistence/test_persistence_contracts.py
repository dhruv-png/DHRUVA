"""The persistence protocols say what ADR-053 and ADR-054 require them to say."""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

import dhruva.shared.persistence as persistence_module
from dhruva.shared.persistence import Repository, TimeSeriesStorage, UnitOfWork

pytestmark = pytest.mark.unit


def test_no_protocol_names_a_persistence_framework() -> None:
    """Rule R7 in miniature: these contracts must be framework-free.

    An application layer depends on these. If a SQLAlchemy type appeared in a
    signature, every consumer would acquire the dependency transitively.
    """
    source = pathlib.Path(persistence_module.__file__ or "").read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Prose explaining which frameworks are excluded is welcome; an executable
    # reference to one is not. Same technique as the identity-module test in S03.
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    referenced: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.append(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.append(node.attr)
        elif isinstance(node, ast.alias):
            referenced.append(node.name)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            referenced.append(node.value)

    haystack = " ".join(referenced).lower()
    for framework in ("sqlalchemy", "asyncpg", "psycopg", "alembic"):
        assert framework not in haystack, f"{framework} reached a persistence contract"


def test_a_repository_cannot_commit() -> None:
    """ADR-053. Transaction lifetime belongs to the Unit of Work.

    Asserted on the protocol rather than on an implementation, because the
    protocol is what an application layer programs against.
    """
    methods = {name for name in dir(Repository) if not name.startswith("_")}

    assert methods == {"get", "add", "update"}
    assert "commit" not in methods
    assert "rollback" not in methods


def test_the_unit_of_work_owns_the_transaction() -> None:
    """It is the only contract exposing commit and rollback."""
    methods = {name for name in dir(UnitOfWork) if not name.startswith("_")}

    assert {"commit", "rollback"} <= methods


def test_the_unit_of_work_is_an_async_context_manager() -> None:
    """Entering begins a transaction; leaving without commit rolls back."""
    assert hasattr(UnitOfWork, "__aenter__")
    assert hasattr(UnitOfWork, "__aexit__")


def test_timeseries_storage_exposes_only_append() -> None:
    """ADR-054, enforced by the shape of the interface.

    No get, no update, no delete, no identity lookup. The absence of those
    methods is what stops this becoming a second general persistence path -- an
    aggregate cannot be loaded, mutated and stored through an interface that
    offers only append.
    """
    methods = {name for name in dir(TimeSeriesStorage) if not name.startswith("_")}

    assert methods == {"append"}


def test_timeseries_storage_deals_in_rows_not_objects() -> None:
    """A typed object on this path could carry domain meaning. A mapping cannot."""
    signature = inspect.signature(TimeSeriesStorage.append)
    rows = signature.parameters["rows"].annotation

    assert "TimeSeriesRow" in str(rows) or "Mapping" in str(rows)


@pytest.mark.parametrize("protocol", [Repository, UnitOfWork, TimeSeriesStorage])
def test_every_contract_is_runtime_checkable(protocol: type) -> None:
    """So a composition root can assert an implementation conforms before wiring it."""
    assert getattr(protocol, "_is_runtime_protocol", False)
