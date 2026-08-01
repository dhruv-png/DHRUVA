"""The decision log is intact, and the guard that proves it actually works.

ADR-027 makes the Architecture Decision Record corpus immutable once accepted.
That promise is only as good as the checker behind it, so the checker is tested
against corpora that deliberately break each rule.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from dhruva.tooling.adr_guard import (
    CHECKSUM_FILENAME,
    REQUIRED_SECTIONS,
    AdrProblem,
    body_digest,
    check_adrs,
    load_adrs,
    update_checksums,
)

VALID = """# ADR-001 — Example decision

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

## Context

Something had to be decided.

## Decision

This was decided.

## Rationale

Because the alternatives were worse.

## Consequences

Some things became easier and some became harder.
"""


def _checks(problems: list[AdrProblem]) -> set[str]:
    return {problem.check for problem in problems}


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    """Return an empty ADR directory."""
    directory = tmp_path / "adr"
    directory.mkdir()
    return directory


def _accept(directory: Path, filename: str, text: str = VALID) -> Path:
    """Write a record and register its checksum, as the real workflow would."""
    path = directory / filename
    path.write_text(text, encoding="utf-8")
    added, problems = update_checksums(directory)
    assert not problems, problems
    assert filename in added
    return path


# --------------------------------------------------------------------------- #
# The live check
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_real_decision_log_is_intact(adr_dir: Path) -> None:
    """The shipped corpus passes every integrity check."""
    problems = check_adrs(adr_dir)
    assert not problems, "\n" + "\n".join(p.render() for p in problems)


#: Matches one row of the decision index in ``docs/decisions.md``.
_INDEX_ROW = re.compile(r"^\|\s*\[ADR-(\d{3})\]\((?P<file>ADR-\d{3}[^)]*\.md)\)\s*\|", re.MULTILINE)


@pytest.mark.unit
def test_the_corpus_and_the_decision_index_agree(adr_dir: Path) -> None:
    """Every record is indexed, every index row has a record, and the numbers are contiguous.

    Cross-checked against ``docs/decisions.md`` rather than against a literal
    range. The previous version asserted ``range(1, 60)``, which was true when it
    was written and became false the moment S05 recorded ADR-060 -- and the
    failure sat unnoticed on the branch, because the assertion described a fact
    about the past rather than an invariant.

    An invariant is what belongs here: a record nobody indexed is a decision
    nobody will find, and an index row with no record is a promise with nothing
    behind it. Both stay true as the corpus grows, and neither needs editing when
    it does.
    """
    records, problems = load_adrs(adr_dir)
    assert not problems

    index = (adr_dir.parent / "decisions.md").read_text(encoding="utf-8")
    indexed = {int(number): filename for number, filename in _INDEX_ROW.findall(index)}
    on_disk = {record.number: record.filename for record in records}

    assert on_disk == indexed, "the corpus and docs/decisions.md have drifted apart"
    numbers = sorted(on_disk)
    assert numbers == list(range(1, len(numbers) + 1)), "ADR numbers must have no gaps"


@pytest.mark.unit
def test_exactly_one_record_has_been_superseded(adr_dir: Path) -> None:
    """ADR-005 is superseded by ADR-042 and retained as history (ADR-027).

    Pinned deliberately. Superseding a record is a heavier act than adding one,
    and a second one appearing without this test being updated would mean it
    happened without anybody deciding to.
    """
    records, _ = load_adrs(adr_dir)

    assert [record.number for record in records if not record.is_accepted] == [5]


@pytest.mark.unit
def test_every_real_record_has_the_required_sections(adr_dir: Path) -> None:
    """A record without a rationale is advocacy, not a decision record."""
    records, _ = load_adrs(adr_dir)
    for record in records:
        missing = [s for s in REQUIRED_SECTIONS if s not in record.sections]
        assert not missing, f"{record.filename} is missing {missing}"


# --------------------------------------------------------------------------- #
# Guard self-tests
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_valid_corpus_passes(corpus: Path) -> None:
    """The happy path stays green."""
    _accept(corpus, "ADR-001-example-decision.md")
    assert check_adrs(corpus) == []


@pytest.mark.unit
def test_c1_rejects_a_malformed_filename(corpus: Path) -> None:
    """Filename discipline keeps the corpus sortable and greppable."""
    (corpus / "adr1.md").write_text(VALID, encoding="utf-8")
    assert "C1" in _checks(check_adrs(corpus))


@pytest.mark.unit
def test_c1_rejects_a_filename_that_disagrees_with_the_heading(corpus: Path) -> None:
    """A mismatch means one of the two is a copy-paste error."""
    (corpus / "ADR-002-example-decision.md").write_text(VALID, encoding="utf-8")
    problems = check_adrs(corpus)
    assert "C1" in _checks(problems)


@pytest.mark.unit
def test_c2_detects_a_gap_left_by_a_deleted_record(corpus: Path) -> None:
    """A deleted decision is a silently modified architecture (ADR-027)."""
    _accept(corpus, "ADR-001-example-decision.md")
    _accept(corpus, "ADR-003-third.md", VALID.replace("ADR-001", "ADR-003"))
    problems = check_adrs(corpus)
    assert "C2" in _checks(problems)
    assert "ADR-002" in " ".join(p.message for p in problems)


@pytest.mark.unit
def test_c3_detects_a_missing_section(corpus: Path) -> None:
    """Structure is what makes the corpus readable years later."""
    (corpus / "ADR-001-example-decision.md").write_text(
        VALID.replace("## Rationale\n\nBecause the alternatives were worse.\n\n", ""),
        encoding="utf-8",
    )
    problems = check_adrs(corpus)
    assert "C3" in _checks(problems)
    assert "Rationale" in " ".join(p.message for p in problems)


@pytest.mark.unit
def test_c3_detects_a_missing_metadata_field(corpus: Path) -> None:
    """Without Deciders, accountability for the decision is unrecorded."""
    text = "\n".join(line for line in VALID.splitlines() if not line.startswith("- **Deciders:**"))
    (corpus / "ADR-001-example-decision.md").write_text(text + "\n", encoding="utf-8")
    assert "C3" in _checks(check_adrs(corpus))


@pytest.mark.unit
def test_c4_rejects_an_invented_status(corpus: Path) -> None:
    """Only four statuses exist; anything else is ambiguous to a reader."""
    (corpus / "ADR-001-example-decision.md").write_text(
        VALID.replace("**Status:** Accepted", "**Status:** Probably fine"), encoding="utf-8"
    )
    assert "C4" in _checks(check_adrs(corpus))


@pytest.mark.unit
def test_c4_rejects_a_dangling_supersession_reference(corpus: Path) -> None:
    """A pointer to a record that does not exist breaks the audit chain."""
    (corpus / "ADR-001-example-decision.md").write_text(
        VALID.replace("**Status:** Accepted", "**Status:** Superseded by ADR-099"),
        encoding="utf-8",
    )
    assert "C4" in _checks(check_adrs(corpus))


@pytest.mark.unit
def test_c5_detects_an_edit_to_an_accepted_record(corpus: Path) -> None:
    """Detecting a silent edit is the entire purpose of ADR-027."""
    path = _accept(corpus, "ADR-001-example-decision.md")
    path.write_text(
        VALID.replace("Because the alternatives were worse.", "Because it felt right."),
        encoding="utf-8",
    )
    problems = check_adrs(corpus)
    assert "C5" in _checks(problems)
    assert "supersedes" in " ".join(p.message for p in problems)


@pytest.mark.unit
def test_c5_permits_a_status_change_to_superseded(corpus: Path) -> None:
    """Superseding is the sanctioned way to change a decision, so it must not trip."""
    path = _accept(corpus, "ADR-001-example-decision.md")
    _accept(corpus, "ADR-002-replacement.md", VALID.replace("ADR-001", "ADR-002"))
    path.write_text(
        VALID.replace("**Status:** Accepted", "**Status:** Superseded by ADR-002").replace(
            "**Superseded by:** —", "**Superseded by:** ADR-002"
        ),
        encoding="utf-8",
    )
    assert check_adrs(corpus) == []


@pytest.mark.unit
def test_c5_detects_an_accepted_record_with_no_registered_checksum(corpus: Path) -> None:
    """Skipping ``--update`` would otherwise leave a record unprotected."""
    (corpus / "ADR-001-example-decision.md").write_text(VALID, encoding="utf-8")
    assert "C5" in _checks(check_adrs(corpus))


@pytest.mark.unit
def test_c5_detects_a_checksum_for_a_record_that_no_longer_exists(corpus: Path) -> None:
    """Deleting the file but leaving the checksum must not pass quietly."""
    path = _accept(corpus, "ADR-001-example-decision.md")
    path.unlink()
    assert "C5" in _checks(check_adrs(corpus))


@pytest.mark.unit
def test_update_refuses_to_overwrite_an_existing_checksum(corpus: Path) -> None:
    """The refusal is the enforcement mechanism, not a convenience feature."""
    path = _accept(corpus, "ADR-001-example-decision.md")
    original = json.loads((corpus / CHECKSUM_FILENAME).read_text(encoding="utf-8"))
    path.write_text(VALID.replace("This was decided.", "That was decided."), encoding="utf-8")

    added, problems = update_checksums(corpus)

    assert added == []
    assert "C5" in _checks(problems)
    assert json.loads((corpus / CHECKSUM_FILENAME).read_text(encoding="utf-8")) == original


@pytest.mark.unit
def test_update_is_idempotent(corpus: Path) -> None:
    """Running the tool twice must not churn the registry."""
    _accept(corpus, "ADR-001-example-decision.md")
    added, problems = update_checksums(corpus)
    assert added == []
    assert problems == []


@pytest.mark.unit
def test_digest_ignores_mutable_status_lines() -> None:
    """Status and supersession are metadata about a decision, not the decision."""
    superseded = VALID.replace("**Status:** Accepted", "**Status:** Superseded by ADR-009").replace(
        "**Superseded by:** —", "**Superseded by:** ADR-009"
    )
    assert body_digest(VALID) == body_digest(superseded)


@pytest.mark.unit
def test_digest_ignores_trailing_whitespace_but_not_content() -> None:
    """An editor save is not a decision change; a reworded rationale is."""
    assert body_digest(VALID) == body_digest(VALID.replace("## Context\n", "## Context   \n"))
    assert body_digest(VALID) != body_digest(VALID.replace("This was decided.", "This was not."))


@pytest.mark.unit
def test_c3_detects_a_record_with_no_heading(corpus: Path) -> None:
    """A record without a heading cannot be identified or indexed."""
    (corpus / "ADR-001-headless.md").write_text(
        VALID.replace("# ADR-001 — Example decision\n", ""), encoding="utf-8"
    )
    problems = check_adrs(corpus)
    assert "C3" in _checks(problems)
    assert "heading" in " ".join(p.message for p in problems)


@pytest.mark.unit
def test_c2_detects_a_reused_decision_number(corpus: Path) -> None:
    """Numbers are never reused; two records claiming one number breaks every link."""
    _accept(corpus, "ADR-001-first.md")
    (corpus / "ADR-001-second.md").write_text(VALID, encoding="utf-8")
    problems = check_adrs(corpus)
    assert "C2" in _checks(problems)
    assert "duplicate" in " ".join(p.message for p in problems)


@pytest.mark.unit
def test_update_declines_when_the_corpus_is_structurally_broken(corpus: Path) -> None:
    """Registering checksums for a malformed corpus would bless the malformation."""
    (corpus / "not-an-adr.md").write_text(VALID, encoding="utf-8")
    added, problems = update_checksums(corpus)
    assert added == []
    assert "C1" in _checks(problems)
    assert not (corpus / CHECKSUM_FILENAME).exists()


@pytest.mark.unit
def test_a_corrupt_checksum_file_fails_loudly(corpus: Path) -> None:
    """Silently treating a corrupt registry as empty would disable the control."""
    _accept(corpus, "ADR-001-example-decision.md")
    (corpus / CHECKSUM_FILENAME).write_text('["not", "an", "object"]', encoding="utf-8")
    with pytest.raises(TypeError, match="JSON object"):
        check_adrs(corpus)


@pytest.mark.unit
def test_problems_render_with_filename_and_check_id(corpus: Path) -> None:
    """Output has to be actionable at a glance in a CI log."""
    (corpus / "ADR-001-example-decision.md").write_text(VALID, encoding="utf-8")
    rendered = check_adrs(corpus)[0].render()
    assert rendered.startswith("ADR-001-example-decision.md: [C5]")


@pytest.mark.unit
def test_readme_and_template_are_not_treated_as_records(corpus: Path) -> None:
    """Supporting documents in the directory must not break numbering checks."""
    _accept(corpus, "ADR-001-example-decision.md")
    (corpus / "README.md").write_text("# Architecture Decision Records\n", encoding="utf-8")
    (corpus / "TEMPLATE.md").write_text("# ADR-nnn - Title\n", encoding="utf-8")
    assert check_adrs(corpus) == []
