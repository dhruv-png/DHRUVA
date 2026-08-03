"""The news domain imports no provider, no framework and no model runtime.

The first news slice is deliberately pure. Every rule here is arithmetic over
text that some adapter will one day hand it, and the moment a feed parser, an
HTTP client or a model runtime appears in this layer, the rules stop being
testable without one. This test is the control on that, and it runs before any
adapter exists so that the boundary is established rather than restored.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

#: Anything that would make the domain need a network, a database or a GPU.
FORBIDDEN_ROOTS = frozenset(
    {
        "aiohttp",
        "alembic",
        "asyncpg",
        "bs4",
        "feedparser",
        "httpx",
        "httpx2",
        "lxml",
        "numpy",
        "onnxruntime",
        "pandas",
        "psycopg",
        "psycopg2",
        "pydantic",
        "requests",
        "safetensors",
        "sqlalchemy",
        "tokenizers",
        "torch",
        "transformers",
        "urllib3",
    }
)

#: The only DHRUVA packages a domain layer may reach (ADR-001).
ALLOWED_DHRUVA_PREFIXES = ("dhruva.shared", "dhruva.contexts.intelligence.domain")


@pytest.fixture(scope="module")
def domain_modules(source_root: Path) -> tuple[Path, ...]:
    """Return every module in the intelligence domain layer."""
    root = source_root / "dhruva" / "contexts" / "intelligence" / "domain"
    modules = tuple(sorted(root.glob("*.py")))
    assert modules, "the intelligence domain layer has no modules"
    return modules


def _imported_roots(module: Path) -> set[str]:
    """Return the top-level package of every import in one module."""
    roots: set[str] = set()
    for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _imported_modules(module: Path) -> set[str]:
    """Return the full dotted name of every absolute import in one module."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def test_no_provider_or_framework_reaches_the_news_domain(
    domain_modules: tuple[Path, ...],
) -> None:
    """A rule that needs a feed parser cannot be tested without one."""
    for module in domain_modules:
        offending = _imported_roots(module) & FORBIDDEN_ROOTS
        assert not offending, f"{module.name} imports {sorted(offending)}"


def test_the_news_domain_reaches_no_other_context(domain_modules: tuple[Path, ...]) -> None:
    """Intelligence may read reference and market data -- but not from its domain."""
    for module in domain_modules:
        for name in _imported_modules(module):
            if not name.startswith("dhruva"):
                continue
            assert name.startswith(ALLOWED_DHRUVA_PREFIXES), f"{module.name} imports {name}"


def test_the_news_domain_holds_no_full_article_body(domain_modules: tuple[Path, ...]) -> None:
    """Unless a provider's terms permit more, DHRUVA stores metadata and a snippet.

    A crude textual scan is the right control here for the same reason it is in
    the order-placement check: it catches the earliest, most innocent-looking
    version of the mistake, which is a field called ``body`` added in a hurry.
    """
    forbidden_fields = ("body:", "full_text:", "article_text:", "content:", "html:")
    for module in domain_modules:
        text = module.read_text(encoding="utf-8")
        for field in forbidden_fields:
            assert field not in text, f"{module.name} declares a field named {field!r}"
