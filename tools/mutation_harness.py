"""Stopgap mutation-testing harness (ADR-049).

**This is not the intended tool.** ADR-049 selects ``mutmut``. It cannot run in
the current verification environment: mutmut 2.x depends on ``parso``, which
cannot parse the ``match`` statement in ``rounding.py``, and mutmut 3.x runs
tests from a copied ``mutants/`` directory in a way that conflicts with the
Python 3.10 compatibility shim this sandbox needs. Both are consequences of not
having a 3.12 interpreter, and both are tracked as technical debt.

This harness exists so that a *real* mutation score is available now rather than
a promise of one later. It is deliberately small enough to audit by reading:
it applies a fixed set of standard mutation operators to one source file at a
time, runs the relevant tests, and records whether the suite noticed.

Delete this file once mutmut runs on a 3.12 host.
"""

from __future__ import annotations

import argparse
import ast
import os
import random
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: Comparison flips. Each pair is mutated in both directions.
_COMPARE_SWAPS: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
}

#: Arithmetic operator swaps.
_BINOP_SWAPS: dict[type[ast.operator], type[ast.operator]] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.FloorDiv,
    ast.FloorDiv: ast.Mult,
    ast.Mod: ast.Mult,
}


@dataclass(frozen=True, slots=True)
class Mutant:
    """One applied mutation."""

    path: Path
    lineno: int
    description: str
    source: str


class _Mutator(ast.NodeTransformer):
    """Applies exactly one mutation, identified by ordinal."""

    def __init__(self, target: int) -> None:
        self.target = target
        self.counter = 0
        self.applied: str | None = None
        self.lineno = 0

    def _take(self, node: ast.AST, description: str) -> bool:
        hit = self.counter == self.target
        if hit:
            self.applied = description
            self.lineno = getattr(node, "lineno", 0)
        self.counter += 1
        return hit

    def visit_Compare(self, node: ast.Compare) -> ast.AST:  # noqa: N802 - visitor protocol
        """Flip a comparison operator."""
        self.generic_visit(node)
        for index, op in enumerate(node.ops):
            replacement = _COMPARE_SWAPS.get(type(op))
            if replacement is not None and self._take(
                node, f"{type(op).__name__}->{replacement.__name__}"
            ):
                node.ops[index] = replacement()
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:  # noqa: N802 - visitor protocol
        """Swap an arithmetic operator."""
        self.generic_visit(node)
        replacement = _BINOP_SWAPS.get(type(node.op))
        if replacement is not None and self._take(
            node, f"{type(node.op).__name__}->{replacement.__name__}"
        ):
            node.op = replacement()
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:  # noqa: N802 - visitor protocol
        """Perturb an integer constant."""
        if (
            type(node.value) is int
            and not isinstance(node.value, bool)
            and self._take(node, f"const {node.value}->{node.value + 1}")
        ):
            return ast.copy_location(ast.Constant(value=node.value + 1), node)
        return node

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:  # noqa: N802 - visitor protocol
        """Remove a logical negation."""
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and self._take(node, "drop not"):
            return node.operand
        return node


def _count_mutations(tree: ast.AST) -> int:
    counter = _Mutator(-1)
    counter.visit(ast.parse(ast.unparse(tree)))
    return counter.counter


def _mutate(source: str, ordinal: int) -> tuple[str, str, int] | None:
    mutator = _Mutator(ordinal)
    mutated = mutator.visit(ast.parse(source))
    if mutator.applied is None:
        return None
    ast.fix_missing_locations(mutated)
    return ast.unparse(mutated), mutator.applied, mutator.lineno


def run(  # noqa: PLR0913 - a harness, not an API
    targets: list[Path], test_paths: list[str], repo: Path, *, sample: int, seed: int
) -> int:
    """Mutate each target in turn and report how many mutants survived.

    ``sample`` bounds how many mutants are attempted per file. Exhaustive
    analysis of these packages is roughly half an hour, so a *seeded* sample
    keeps the run affordable while remaining reproducible: the same seed always
    selects the same mutants, so a score can be compared across runs.
    """
    killed = 0
    survivors: list[Mutant] = []
    attempted = 0
    population = 0

    scratch_root = Path(tempfile.mkdtemp(prefix="dhruva-mutants-"))
    scratch = scratch_root / repo.name
    shutil.copytree(
        repo,
        scratch,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".hypothesis", "*.lock"),
    )
    sys.stdout.write(f"scratch tree: {scratch}\n")

    for target in targets:
        original = target.read_text(encoding="utf-8")
        total = _count_mutations(ast.parse(original))
        population += total
        chosen = sorted(random.Random(seed).sample(range(total), min(sample, total)))
        sys.stdout.write(f"{target.relative_to(repo)}: {total} mutants, sampling {len(chosen)}\n")
        sys.stdout.flush()

        for ordinal in chosen:
            attempted += 1
            result = _mutate(original, ordinal)
            if result is None:
                continue
            mutated_source, description, lineno = result

            # Written into a scratch copy of the tree, never over the original.
            # An earlier version mutated in place with a try/finally restore; a
            # timeout killed the process before the finally ran and left a
            # mutated, comment-stripped source file on disk. Working on a copy
            # makes that failure mode impossible rather than unlikely.
            scratch_target = scratch / target.relative_to(repo)
            scratch_target.write_text(mutated_source, encoding="utf-8")
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-x",
                    "-q",
                    "--no-header",
                    "-p",
                    "no:randomly",
                    "-p",
                    "no:cacheprovider",
                    *test_paths,
                ],
                cwd=scratch / "backend",
                capture_output=True,
                timeout=180,
                check=False,
                env={**os.environ, "DHRUVA_FAST_PROPERTY_TESTS": "1"},
            )
            scratch_target.write_text(original, encoding="utf-8")

            if completed.returncode == 0:
                survivors.append(Mutant(target, lineno, description, mutated_source))
                sys.stdout.write(f"  SURVIVED line {lineno}: {description}\n")
            else:
                killed += 1
        sys.stdout.flush()

    score = (killed / attempted * 100) if attempted else 100.0
    sys.stdout.write(
        f"\nmutation score: {score:.1f}%  ({killed} killed, {len(survivors)} survived, "
        f"{attempted} attempted of {population} possible, seed={seed})\n"
    )
    return 0 if score >= 90.0 else 1


def main() -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description="Stopgap mutation harness (ADR-049).")
    parser.add_argument("--package", required=True, help="package directory to mutate")
    parser.add_argument("--tests", required=True, nargs="+", help="test paths to run")
    parser.add_argument("--sample", type=int, default=40, help="mutants per file")
    parser.add_argument("--seed", type=int, default=20260726, help="sampling seed")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parent.parent
    package = repo / "backend" / args.package
    targets = sorted(p for p in package.rglob("*.py") if p.name != "__init__.py")
    return run(targets, args.tests, repo, sample=args.sample, seed=args.seed)


if __name__ == "__main__":
    raise SystemExit(main())
