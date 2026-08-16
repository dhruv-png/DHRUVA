"""Atomic offline historical import, idempotence and provenance integration."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from dhruva.contexts.marketdata.infrastructure.persistence.models import (
    DailyMarketBarRevisionModel,
)
from dhruva.contexts.platform.infrastructure.database.engine import build_session_factory
from dhruva.contexts.reference.infrastructure.persistence.models import (
    HistoricalDatasetFactProvenanceModel,
    HistoricalDatasetFileModel,
    HistoricalDatasetModel,
    HistoricalImportRunModel,
    InstrumentIdentityRevisionModel,
    ReferenceInstrumentModel,
)
from dhruva.ingest.historical_import import import_historical_dataset
from dhruva.ingest.synthetic_historical_dataset import generate_synthetic_dataset
from dhruva.shared.errors import ConflictError
from dhruva.shared.identity import AccountId

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

ACCOUNT = AccountId.deterministic("historical-import-test")


async def test_ready_pack_import_is_atomic_provenanced_and_idempotent(
    migrated: AsyncEngine,
    tmp_path: Path,
    truncated_after_test: None,  # noqa: ARG001 - cleans committed import rows
) -> None:
    """One delivery creates every fact once and an identical retry adds nothing."""
    manifest = generate_synthetic_dataset(tmp_path / "delivery")
    sessions = build_session_factory(migrated)

    first = await import_historical_dataset(manifest, account_id=ACCOUNT, session_factory=sessions)
    second = await import_historical_dataset(manifest, account_id=ACCOUNT, session_factory=sessions)

    assert first.export_bytes() == second.export_bytes()
    async with sessions() as session:
        models = (
            (HistoricalDatasetModel, 1),
            (HistoricalDatasetFileModel, 6),
            (HistoricalDatasetFactProvenanceModel, 5046),
            (HistoricalImportRunModel, 1),
            (ReferenceInstrumentModel, 7),
            (InstrumentIdentityRevisionModel, 8),
            (DailyMarketBarRevisionModel, 5025),
        )
        for model, expected in models:
            observed = await session.scalar(select(func.count()).select_from(model))
            assert observed == expected


async def test_dataset_ledger_refuses_mutation(
    migrated: AsyncEngine,
) -> None:
    """Database triggers keep accepted delivery evidence append-only."""
    sessions = build_session_factory(migrated)
    async with sessions() as session:
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(
                text(
                    "TRUNCATE historical_import_run, "
                    "historical_dataset_fact_provenance, historical_dataset_file, "
                    "historical_dataset"
                )
            )


async def test_mid_apply_conflict_rolls_back_every_context(
    migrated: AsyncEngine,
    tmp_path: Path,
) -> None:
    """A source-revision conflict after reference writes leaves no partial rows."""
    manifest = generate_synthetic_dataset(tmp_path / "conflict")
    decoded = json.loads(manifest.read_text(encoding="utf-8"))
    actions = tmp_path / "conflict" / "corporate_actions.csv"
    with actions.open("r", encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
        fields = tuple(rows[0])
    rows[3]["source_revision"] = rows[2]["source_revision"]
    with actions.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    content = actions.read_bytes()
    entry = next(item for item in decoded["files"] if item["role"] == "corporate_actions")
    entry["sha256"] = hashlib.sha256(content).hexdigest()
    entry["size_bytes"] = len(content)
    manifest.write_text(json.dumps(decoded), encoding="utf-8")
    sessions = build_session_factory(migrated)

    with pytest.raises(ConflictError, match="source revision"):
        await import_historical_dataset(manifest, account_id=ACCOUNT, session_factory=sessions)

    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(HistoricalDatasetModel)) == 0
        assert await session.scalar(select(func.count()).select_from(ReferenceInstrumentModel)) == 0
