"""Offline intake contract, synthetic sample and diligence evidence tests."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from dhruva.ingest.historical_dataset import ImportDecision, preflight_dataset
from dhruva.ingest.historical_mapper import (
    CanonicalCsvMapper,
    DatasetMapper,
    SyntheticFixtureMapper,
)
from dhruva.ingest.provider_diligence import (
    PROVIDERS,
    ProviderDisposition,
    diligence_export_bytes,
)
from dhruva.ingest.synthetic_historical_dataset import generate_synthetic_dataset
from dhruva.workers.data import run


def test_synthetic_pack_is_ready_and_deterministic(tmp_path: Path) -> None:
    """The valid fixture reaches READY with byte-stable evidence."""
    manifest = generate_synthetic_dataset(tmp_path / "good")

    first = preflight_dataset(manifest)
    second = preflight_dataset(manifest)

    assert first.import_decision is ImportDecision.READY
    assert first.failures == ()
    assert first.blockers == ()
    assert first.export_bytes() == second.export_bytes()
    assert dict(first.counts) == {
        "benchmark_bars": 783,
        "corporate_actions": 6,
        "daily_bars": 4242,
        "instrument_identities": 7,
        "universe_definitions": 1,
        "universe_memberships": 7,
    }


def test_offline_mapper_contract_has_canonical_and_synthetic_implementations(
    tmp_path: Path,
) -> None:
    """Both required mapper implementations use the same versioned seam."""
    synthetic: DatasetMapper = SyntheticFixtureMapper()
    generated = synthetic.map(tmp_path / "mapped")
    canonical: DatasetMapper = CanonicalCsvMapper()
    selected = canonical.map(generated.manifest_path)

    assert isinstance(synthetic, DatasetMapper)
    assert isinstance(canonical, DatasetMapper)
    assert generated.import_decision is ImportDecision.READY
    assert selected.dataset_fingerprint == generated.dataset_fingerprint


def test_corrupted_pack_is_rejected_before_semantic_import(tmp_path: Path) -> None:
    """A post-manifest payload change fails its cryptographic envelope."""
    manifest = generate_synthetic_dataset(tmp_path / "bad", corrupt=True)

    report = preflight_dataset(manifest)

    assert report.import_decision is ImportDecision.REJECTED
    assert "FILE_HASH_MISMATCH" in {item.code for item in report.failures}


def test_semantic_preflight_accumulates_multiple_row_failures(tmp_path: Path) -> None:
    """Independent bad rows are all visible in one preflight pass."""
    manifest = generate_synthetic_dataset(tmp_path / "semantic")
    decoded = json.loads(manifest.read_text(encoding="utf-8"))
    daily = tmp_path / "semantic" / "daily_bars.csv"
    with daily.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))
    rows[1][4] = "-1"
    rows[2][4] = "-2"
    with daily.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream, lineterminator="\n").writerows(rows)
    content = daily.read_bytes()
    file_entry = next(item for item in decoded["files"] if item["role"] == "daily_bars")
    file_entry["sha256"] = hashlib.sha256(content).hexdigest()
    file_entry["size_bytes"] = len(content)
    manifest.write_text(json.dumps(decoded), encoding="utf-8")

    report = preflight_dataset(manifest)

    invalid_rows = [item for item in report.failures if item.code == "ROW_SEMANTIC_INVALID"]
    assert len(invalid_rows) >= 2
    assert report.import_decision is ImportDecision.QUARANTINED


def test_path_traversal_manifest_is_rejected(tmp_path: Path) -> None:
    """Payloads cannot escape the selected delivery directory."""
    manifest = generate_synthetic_dataset(tmp_path / "traversal")
    decoded = json.loads(manifest.read_text(encoding="utf-8"))
    decoded["files"][0]["path"] = "../outside.csv"
    manifest.write_text(json.dumps(decoded), encoding="utf-8")

    report = preflight_dataset(manifest)

    assert report.import_decision is ImportDecision.REJECTED
    assert report.failures[0].code == "PATH_TRAVERSAL"


def test_unconfirmed_retention_is_quarantined(tmp_path: Path) -> None:
    """Technically valid bytes cannot elevate their own licensing state."""
    manifest = generate_synthetic_dataset(tmp_path / "licensing")
    decoded = json.loads(manifest.read_text(encoding="utf-8"))
    decoded["license"]["status"] = "EVALUATION_ONLY"
    definitions = tmp_path / "licensing" / "universe_definitions.csv"
    with definitions.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))
    licensing_column = rows[0].index("licensing_confirmed")
    rows[1][licensing_column] = "false"
    with definitions.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream, lineterminator="\n").writerows(rows)
    content = definitions.read_bytes()
    entry = next(item for item in decoded["files"] if item["role"] == "universe_definitions")
    entry["sha256"] = hashlib.sha256(content).hexdigest()
    entry["size_bytes"] = len(content)
    manifest.write_text(json.dumps(decoded), encoding="utf-8")

    report = preflight_dataset(manifest)

    assert report.import_decision is ImportDecision.QUARANTINED
    assert "OWNER_LOCAL_RETENTION_NOT_CONFIRMED" in report.blockers


def test_diligence_export_never_approves_a_provider() -> None:
    """Public-source scoring never becomes a purchase or approval decision."""
    payload = json.loads(diligence_export_bytes())

    assert payload["approval_status"] == "NO_PROVIDER_APPROVED"
    assert payload["owner_diligence_shortlist"] == [
        "nse_data",
        "factset",
        "lseg",
        "globaldatafeeds",
    ]
    assert len(PROVIDERS) >= 12
    assert all(0 <= item.weighted_score <= 100 for item in PROVIDERS)
    assert all(item.disposition is not ProviderDisposition.SHORTLIST for item in PROVIDERS)
    assert all(item.blocking_unknowns for item in PROVIDERS)


@pytest.mark.asyncio
async def test_cli_import_refuses_without_explicit_apply(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The write path requires the explicit apply acknowledgement."""
    manifest = generate_synthetic_dataset(tmp_path / "cli")

    exit_code = await run(["import", "--manifest", str(manifest), "--account", "owner"])

    assert exit_code == 2
    assert "requires --apply" in capsys.readouterr().out
