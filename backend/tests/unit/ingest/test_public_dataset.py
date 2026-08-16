"""End-to-end public reconstruction into the canonical offline contract."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from dhruva.ingest.historical_dataset import (
    EvidenceClass,
    ImportDecision,
    load_manifest,
    preflight_dataset,
)
from dhruva.ingest.historical_mapper import DatasetMapper
from dhruva.ingest.public_dataset import NsePublicDropMapper, build_public_dataset
from dhruva.ingest.public_reconstruction import inspect_drop


def _sessions(count: int) -> tuple[date, ...]:
    result: list[date] = []
    current = date(2024, 1, 1)
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return tuple(result)


def _drop(root: Path, *, sessions: int = 60) -> Path:
    root.mkdir(parents=True)
    days = _sessions(sessions)
    with (root / "BhavCopy_NSE_CM_0_0_0_20240101_F_0000.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        columns = (
            "TradDt",
            "BizDt",
            "Sgmt",
            "Src",
            "FinInstrmTp",
            "FinInstrmId",
            "ISIN",
            "TckrSymb",
            "SctySrs",
            "OpnPric",
            "HghPric",
            "LwPric",
            "ClsPric",
            "TtlTradgVol",
            "TtlTrfVal",
            "TtlNbOfTxsExctd",
        )
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for index, day in enumerate(days):
            writer.writerow(
                {
                    "TradDt": day,
                    "BizDt": day,
                    "Sgmt": "CM",
                    "Src": "NSE",
                    "FinInstrmTp": "STK",
                    "FinInstrmId": "101",
                    "ISIN": "INE000A01001",
                    "TckrSymb": "ALPHA",
                    "SctySrs": "EQ",
                    "OpnPric": 100 + index,
                    "HghPric": 102 + index,
                    "LwPric": 99 + index,
                    "ClsPric": 101 + index,
                    "TtlTradgVol": 200000,
                    "TtlTrfVal": 20_000_000 + index,
                    "TtlNbOfTxsExctd": 500,
                }
            )
    (root / "NSE_CM_security_01012024.csv").write_text(
        "FinInstrmId,TckrSymb,SctySrs,ISIN,FinInstrmNm,SctySts,ListgDt\n"
        "101,ALPHA,EQ,INE000A01001,Alpha Industries,ACTIVE,2020-01-01\n",
        encoding="utf-8",
    )
    (root / "corporate-actions.csv").write_text(
        "SYMBOL,COMPANY NAME,SERIES,PURPOSE,FACE VALUE,EX-DATE,RECORD DATE\n"
        "ALPHA,Alpha Industries,EQ,Bonus 2:1,10,08-Jan-2024,09-Jan-2024\n",
        encoding="utf-8",
    )
    with (root / "nifty-price.csv").open("w", encoding="utf-8", newline="") as stream:
        benchmark_writer = csv.writer(stream, lineterminator="\n")
        benchmark_writer.writerow(("Date", "Open", "High", "Low", "Close", "Shares Traded"))
        for index, day in enumerate(days):
            benchmark_writer.writerow(
                (
                    day.strftime("%d-%b-%Y"),
                    20000 + index,
                    20100 + index,
                    19900 + index,
                    20050 + index,
                    0,
                )
            )
    return root


def test_public_build_is_deterministic_ready_and_honestly_classified(tmp_path: Path) -> None:
    """Repeated builds are byte-stable and never overclaim PIT evidence."""
    drop = _drop(tmp_path / "drop")
    source_hashes = {
        item.name: hashlib.sha256(item.read_bytes()).hexdigest() for item in drop.iterdir()
    }
    retrieved = datetime(2026, 8, 16, 12, tzinfo=UTC)
    first = build_public_dataset(
        inspect_drop(drop), destination=tmp_path / "first", retrieved_at=retrieved
    )
    second = build_public_dataset(
        inspect_drop(drop), destination=tmp_path / "second", retrieved_at=retrieved
    )
    assert first.dataset_fingerprint == second.dataset_fingerprint
    assert first.canonical_manifest.read_bytes() == second.canonical_manifest.read_bytes()
    report = preflight_dataset(first.canonical_manifest)
    assert report.import_decision is ImportDecision.READY
    _root, manifest = load_manifest(first.canonical_manifest)
    assert manifest.evidence_class is EvidenceClass.PUBLIC_RECONSTRUCTED
    assert manifest.known_at_semantics == "RETRIEVED_LATER"
    assert manifest.return_basis == "RAW_PRICE"
    assert not manifest.capabilities.pit_known_at
    reconstruction = json.loads(first.reconstruction_manifest.read_bytes())
    assert "not institutional-grade PIT or survivorship-safe" in reconstruction["limitations"]
    assert source_hashes == {
        item.name: hashlib.sha256(item.read_bytes()).hexdigest() for item in drop.iterdir()
    }


def test_public_drop_mapper_implements_existing_offline_seam(tmp_path: Path) -> None:
    """The public mapper plugs into the established offline mapping seam."""
    mapper = NsePublicDropMapper(retrieved_at=datetime(2026, 8, 16, tzinfo=UTC))
    assert isinstance(mapper, DatasetMapper)
    result = mapper.map(_drop(tmp_path / "drop"), destination=tmp_path / "output")
    assert result.mapper_id == "nse_public_drop"
    assert result.import_decision is ImportDecision.READY


def test_public_build_retains_raw_prices_and_explicit_action_only(tmp_path: Path) -> None:
    """Public output keeps raw bars and only parses actions stated by the source."""
    result = build_public_dataset(
        inspect_drop(_drop(tmp_path / "drop")),
        destination=tmp_path / "output",
        retrieved_at=datetime(2026, 8, 16, tzinfo=UTC),
    )
    output = result.canonical_manifest.parent
    with (output / "daily_bars.csv").open(encoding="utf-8", newline="") as stream:
        bars = tuple(csv.DictReader(stream))
    with (output / "corporate_actions.csv").open(encoding="utf-8", newline="") as stream:
        actions = tuple(csv.DictReader(stream))
    assert {item["adjustment_status"] for item in bars} == {"RAW"}
    assert actions[0]["event_type"] == "BONUS"
    assert (actions[0]["ratio_numerator"], actions[0]["ratio_denominator"]) == ("2", "1")
    with (output / "security-observations.csv").open(encoding="utf-8", newline="") as stream:
        observations = tuple(csv.DictReader(stream))
    assert len(observations) == 60
    assert {item["state"] for item in observations} == {"OBSERVED_TRADED"}
    assert {item["disappearance_state"] for item in observations} == {""}


def test_public_build_requires_selected_official_benchmark_evidence(tmp_path: Path) -> None:
    """A selected benchmark basis must be backed by an official input file."""
    drop = _drop(tmp_path / "drop")
    (drop / "nifty-price.csv").unlink()
    with pytest.raises(ValueError, match="selected benchmark basis"):
        build_public_dataset(
            inspect_drop(drop),
            destination=tmp_path / "output",
            retrieved_at=datetime(2026, 8, 16, tzinfo=UTC),
        )


def test_public_build_fails_closed_on_ambiguous_symbol_identity(tmp_path: Path) -> None:
    """Conflicting symbol identities cannot silently contaminate the dataset."""
    drop = _drop(tmp_path / "drop", sessions=1)
    security = drop / "NSE_CM_security_01012024.csv"
    security.write_text(
        security.read_text() + "102,ALPHA,EQ,INE000B01009,Other Alpha,ACTIVE,2020-01-01\n",
        encoding="utf-8",
    )
    bhav = drop / "BhavCopy_NSE_CM_0_0_0_20240101_F_0000.csv"
    bhav.write_text(
        "SYMBOL,SERIES,DATE1,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,CLOSE_PRICE,"
        "TTL_TRD_QNTY,DELIV_QTY\nALPHA,EQ,01-Jan-2024,100,102,99,101,200000,100000\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unresolved/ambiguous"):
        build_public_dataset(
            inspect_drop(drop),
            destination=tmp_path / "output",
            retrieved_at=datetime(2026, 8, 16, tzinfo=UTC),
        )
