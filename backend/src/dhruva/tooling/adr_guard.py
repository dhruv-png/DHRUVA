"""Architecture Decision Record integrity and immutability guard.

Implements the mechanical enforcement promised by ADR-027 (Amendment A5):
*every major architectural decision is recorded as an ADR, and approved
architecture is never silently modified.*

Checks performed
----------------
C1  **Filename discipline.** Every record is ``ADR-<nnn>-<slug>.md`` with a
    three-digit number, and the number in the filename matches the heading.
C2  **Numbering integrity.** Numbers are unique and contiguous from 001. A gap
    means a record was deleted, which is exactly what ADR-027 forbids.
C3  **Structure.** Every record carries the required metadata fields and the
    four required sections.
C4  **Status validity.** The status is one of the four permitted values, and a
    ``Superseded by`` reference points at a record that exists.
C5  **Immutability.** The body of an ``Accepted`` record -- everything except
    its mutable status lines -- must hash to the value recorded in
    ``docs/adr/checksums.json``.

Why a checksum file rather than trusting git history
-----------------------------------------------------
Git history can be rewritten, and a reviewer will not notice a one-word change
inside a 40-line rationale during a large diff. A checksum turns a silent edit
into a build failure with a named cause. The checksum file is itself reviewed,
so changing a decision requires an obvious, deliberate, visible act.

Workflow
--------
Adding a record: write it, then run ``dhruva-adr-guard --update`` to register
its checksum. The guard will **refuse** to overwrite an existing checksum;
superseding a decision means writing a new record, not editing the old one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from dhruva.tooling.boundaries import find_repo_root

__all__ = [
    "CHECKSUM_FILENAME",
    "NON_RECORD_FILES",
    "REQUIRED_SECTIONS",
    "VALID_STATUSES",
    "AdrProblem",
    "AdrRecord",
    "body_digest",
    "check_adrs",
    "load_adrs",
    "main",
    "update_checksums",
]

#: Permitted values of the ``Status`` field. ``Superseded by ADR-nnn`` carries a
#: reference, so it is matched as a prefix.
VALID_STATUSES: tuple[str, ...] = ("Proposed", "Accepted", "Deprecated", "Superseded by")

#: Sections every record must contain, in this order.
REQUIRED_SECTIONS: tuple[str, ...] = ("Context", "Decision", "Rationale", "Consequences")

#: Metadata fields every record must declare.
REQUIRED_FIELDS: tuple[str, ...] = ("Status", "Date", "Deciders", "Supersedes", "Superseded by")

#: Fields excluded from the immutability digest, because they legitimately
#: change over a record's life without the decision itself changing.
MUTABLE_FIELDS: frozenset[str] = frozenset({"Status", "Superseded by"})

CHECKSUM_FILENAME = "checksums.json"

#: Supporting documents that live alongside the records but are not records.
#: Deliberately an explicit allowlist rather than a pattern: skipping anything
#: that merely fails to look like a record would let a typo'd filename escape
#: check C1, which is the opposite of what the guard is for.
NON_RECORD_FILES: frozenset[str] = frozenset({"README.md", "TEMPLATE.md", "index.md"})

_FILENAME_RE = re.compile(r"^ADR-(\d{3})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
_HEADING_RE = re.compile(r"^#\s+ADR-(\d{3})\s+[-—]\s+(?P<title>.+?)\s*$")
_FIELD_RE = re.compile(r"^-\s+\*\*(?P<name>[A-Za-z ]+):\*\*\s*(?P<value>.*?)\s*$")
_SECTION_RE = re.compile(r"^##\s+(?P<name>.+?)\s*$")
_SUPERSEDED_RE = re.compile(r"^Superseded by ADR-(\d{3})$")


@dataclass(frozen=True, slots=True)
class AdrRecord:
    """A parsed Architecture Decision Record.

    Attributes
    ----------
    path
        Absolute path to the record.
    number
        Decision number taken from the filename.
    title
        Title taken from the level-one heading.
    fields
        Metadata fields declared beneath the heading.
    sections
        Level-two section names, in document order.
    digest
        SHA-256 of the immutable body (see :func:`body_digest`).
    """

    path: Path
    number: int
    title: str
    fields: dict[str, str]
    sections: tuple[str, ...]
    digest: str

    @property
    def filename(self) -> str:
        """Return the record's filename."""
        return self.path.name

    @property
    def status(self) -> str:
        """Return the declared status, or an empty string if absent."""
        return self.fields.get("Status", "")

    @property
    def is_accepted(self) -> bool:
        """Return whether this record is currently binding."""
        return self.status == "Accepted"


@dataclass(frozen=True, slots=True)
class AdrProblem:
    """A single ADR integrity failure.

    Attributes
    ----------
    filename
        Record the problem was found in, or ``docs/adr`` for corpus-wide issues.
    check
        Identifier of the failed check, one of ``C1``..``C5``.
    message
        Actionable description of what is wrong and how to fix it.
    """

    filename: str
    check: str
    message: str

    def render(self) -> str:
        """Return a compact representation of the problem."""
        return f"{self.filename}: [{self.check}] {self.message}"


def body_digest(text: str) -> str:
    """Return the SHA-256 of a record's immutable body.

    Mutable metadata lines -- ``Status`` and ``Superseded by`` -- are excluded,
    so a record can legitimately be marked superseded without tripping the
    immutability check. Every other byte is covered. Trailing whitespace is
    normalised so that a stray editor save is not reported as a decision change.
    """
    kept: list[str] = []
    for raw in text.splitlines():
        match = _FIELD_RE.match(raw)
        if match is not None and match.group("name").strip() in MUTABLE_FIELDS:
            continue
        kept.append(raw.rstrip())
    normalised = "\n".join(kept).strip() + "\n"
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _parse(path: Path) -> tuple[AdrRecord | None, list[AdrProblem]]:
    """Parse one record, returning it and any structural problems found."""
    name = path.name
    problems: list[AdrProblem] = []

    filename_match = _FILENAME_RE.match(name)
    if filename_match is None:
        problems.append(
            AdrProblem(
                filename=name,
                check="C1",
                message=(
                    "filename must be 'ADR-<nnn>-<kebab-case-slug>.md', for example "
                    "'ADR-007-bitemporal-data.md'"
                ),
            )
        )
        return None, problems

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    heading_match = next((m for m in (_HEADING_RE.match(line) for line in lines) if m), None)
    if heading_match is None:
        problems.append(
            AdrProblem(
                filename=name,
                check="C3",
                message="missing a level-one heading of the form '# ADR-nnn - Title'",
            )
        )
        return None, problems

    file_number = int(filename_match.group(1))
    heading_number = int(heading_match.group(1))
    if file_number != heading_number:
        problems.append(
            AdrProblem(
                filename=name,
                check="C1",
                message=(
                    f"filename says ADR-{file_number:03d} but the heading says "
                    f"ADR-{heading_number:03d}"
                ),
            )
        )

    fields: dict[str, str] = {}
    for line in lines:
        field_match = _FIELD_RE.match(line)
        if field_match is not None:
            fields[field_match.group("name").strip()] = field_match.group("value").strip()

    sections = tuple(
        m.group("name").strip() for m in (_SECTION_RE.match(line) for line in lines) if m
    )

    for required in REQUIRED_FIELDS:
        if required not in fields:
            problems.append(
                AdrProblem(
                    filename=name,
                    check="C3",
                    message=f"missing required metadata field '- **{required}:** ...'",
                )
            )
    missing_sections = [s for s in REQUIRED_SECTIONS if s not in sections]
    if missing_sections:
        problems.append(
            AdrProblem(
                filename=name,
                check="C3",
                message=f"missing required section(s): {', '.join(missing_sections)}",
            )
        )

    record = AdrRecord(
        path=path,
        number=file_number,
        title=heading_match.group("title"),
        fields=fields,
        sections=sections,
        digest=body_digest(text),
    )
    return record, problems


def load_adrs(adr_dir: Path) -> tuple[list[AdrRecord], list[AdrProblem]]:
    """Parse every record in ``adr_dir``.

    Returns
    -------
    tuple[list[AdrRecord], list[AdrProblem]]
        Records sorted by number, and any structural problems encountered.
    """
    records: list[AdrRecord] = []
    problems: list[AdrProblem] = []
    for path in sorted(adr_dir.glob("*.md")):
        if path.name in NON_RECORD_FILES:
            continue
        record, found = _parse(path)
        problems.extend(found)
        if record is not None:
            records.append(record)
    records.sort(key=lambda r: r.number)
    return records, problems


def _check_numbering(records: Sequence[AdrRecord]) -> list[AdrProblem]:
    """Apply check C2: numbers are unique and contiguous from 001."""
    problems: list[AdrProblem] = []
    seen: dict[int, str] = {}
    for record in records:
        if record.number in seen:
            problems.append(
                AdrProblem(
                    filename=record.filename,
                    check="C2",
                    message=(
                        f"duplicate decision number {record.number:03d}, already used by "
                        f"'{seen[record.number]}'. Numbers are never reused."
                    ),
                )
            )
        seen[record.number] = record.filename
    expected = list(range(1, len(seen) + 1))
    missing = sorted(set(expected) - set(seen))
    if missing:
        gaps = ", ".join(f"ADR-{n:03d}" for n in missing)
        problems.append(
            AdrProblem(
                filename="docs/adr",
                check="C2",
                message=(
                    f"gap in the decision log: {gaps} absent. A deleted record is a silently "
                    f"modified architecture (ADR-027); supersede it instead."
                ),
            )
        )
    return problems


def _check_statuses(records: Sequence[AdrRecord]) -> list[AdrProblem]:
    """Apply check C4: status values are valid and references resolve."""
    problems: list[AdrProblem] = []
    numbers = {record.number for record in records}
    for record in records:
        status = record.status
        if not status.startswith(VALID_STATUSES):
            problems.append(
                AdrProblem(
                    filename=record.filename,
                    check="C4",
                    message=(
                        f"status '{status}' is not permitted. Use one of: Proposed, Accepted, "
                        f"Deprecated, 'Superseded by ADR-nnn'."
                    ),
                )
            )
            continue
        superseded = _SUPERSEDED_RE.match(status)
        if superseded is not None and int(superseded.group(1)) not in numbers:
            problems.append(
                AdrProblem(
                    filename=record.filename,
                    check="C4",
                    message=(
                        f"declares supersession by ADR-{superseded.group(1)}, which does not exist"
                    ),
                )
            )
    return problems


def _check_immutability(
    records: Sequence[AdrRecord], checksums: dict[str, str]
) -> list[AdrProblem]:
    """Apply check C5: accepted records still hash to their registered digest."""
    problems: list[AdrProblem] = []
    for record in records:
        registered = checksums.get(record.filename)
        if registered is None:
            if record.is_accepted:
                problems.append(
                    AdrProblem(
                        filename=record.filename,
                        check="C5",
                        message=(
                            "accepted but not registered in checksums.json. Run "
                            "'dhruva-adr-guard --update' and commit the result."
                        ),
                    )
                )
            continue
        if registered != record.digest:
            problems.append(
                AdrProblem(
                    filename=record.filename,
                    check="C5",
                    message=(
                        "body has changed since it was accepted. ADR-027 forbids editing an "
                        "accepted decision: write a new ADR that supersedes this one, and set "
                        "this record's status to 'Superseded by ADR-nnn'. Only the Status and "
                        "'Superseded by' lines may change."
                    ),
                )
            )
    stale = sorted(set(checksums) - {r.filename for r in records})
    problems.extend(
        AdrProblem(
            filename=filename,
            check="C5",
            message="registered in checksums.json but the record no longer exists",
        )
        for filename in stale
    )
    return problems


def _load_checksums(adr_dir: Path) -> dict[str, str]:
    """Read the checksum registry, returning an empty mapping if absent."""
    path = adr_dir / CHECKSUM_FILENAME
    if not path.is_file():
        return {}
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        msg = f"{path} must contain a JSON object mapping filename to digest"
        raise TypeError(msg)
    return {str(key): str(value) for key, value in loaded.items()}


def check_adrs(adr_dir: Path) -> list[AdrProblem]:
    """Run all five checks over the record corpus in ``adr_dir``.

    Returns
    -------
    list[AdrProblem]
        Empty when the decision log is intact.
    """
    records, problems = load_adrs(adr_dir)
    problems.extend(_check_numbering(records))
    problems.extend(_check_statuses(records))
    problems.extend(_check_immutability(records, _load_checksums(adr_dir)))
    return problems


def update_checksums(adr_dir: Path) -> tuple[list[str], list[AdrProblem]]:
    """Register digests for records that do not yet have one.

    Existing entries are never overwritten: that refusal is the mechanism by
    which ADR-027 is enforced.

    Returns
    -------
    tuple[list[str], list[AdrProblem]]
        Filenames newly registered, and problems that blocked the update.
    """
    records, problems = load_adrs(adr_dir)
    if problems:
        return [], problems

    checksums = _load_checksums(adr_dir)
    added: list[str] = []
    conflicts: list[AdrProblem] = []
    for record in records:
        registered = checksums.get(record.filename)
        if registered is None:
            checksums[record.filename] = record.digest
            added.append(record.filename)
        elif registered != record.digest:
            conflicts.append(
                AdrProblem(
                    filename=record.filename,
                    check="C5",
                    message=(
                        "refusing to overwrite an existing checksum. An accepted decision is "
                        "immutable; supersede it with a new ADR instead."
                    ),
                )
            )
    if conflicts:
        return [], conflicts

    ordered = dict(sorted(checksums.items()))
    (adr_dir / CHECKSUM_FILENAME).write_text(
        json.dumps(ordered, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return added, []


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ADR guard as a command-line tool.

    Returns
    -------
    int
        ``0`` when the decision log is intact, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(
        prog="dhruva-adr-guard",
        description="Verify Architecture Decision Record integrity and immutability (ADR-027).",
    )
    parser.add_argument(
        "--adr-dir",
        type=Path,
        default=None,
        help="directory holding the records (default: <repo>/docs/adr)",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="register checksums for new records; never overwrites an existing entry",
    )
    args = parser.parse_args(argv)

    adr_dir: Path = args.adr_dir or find_repo_root() / "docs" / "adr"

    if args.update:
        added, problems = update_checksums(adr_dir)
        if problems:
            sys.stdout.write("adr-guard: update refused\n\n")
            for problem in problems:
                sys.stdout.write(f"{problem.render()}\n")
            return 1
        if added:
            sys.stdout.write(f"adr-guard: registered {len(added)} new record(s)\n")
            for filename in added:
                sys.stdout.write(f"  + {filename}\n")
        else:
            sys.stdout.write("adr-guard: nothing to register\n")
        return 0

    problems = check_adrs(adr_dir)
    if not problems:
        sys.stdout.write("adr-guard: OK - decision log intact\n")
        return 0

    sys.stdout.write(f"adr-guard: {len(problems)} problem(s)\n\n")
    for problem in problems:
        sys.stdout.write(f"{problem.render()}\n")
    sys.stdout.write("\nSee docs/adr/README.md for the ADR lifecycle (ADR-027).\n")
    return 1


if __name__ == "__main__":  # pragma: no cover - exercised via the console script
    raise SystemExit(main())
