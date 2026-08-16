"""``dhruva-marketdata`` against real PostgreSQL: coverage, refresh, and refusal.

No live Zerodha call anywhere in this file. ``coverage`` never needs one, and
every ``refresh`` test either exercises the SESSION-credential gate with
nothing enrolled (so it refuses before any adapter is even built) or injects a
synthetic provider through ``run()``'s own seam -- the same one a real
``KiteInstrumentMasterAdapter``/``KiteDailyHistoryAdapter`` pair would occupy.

The Kite instrument-master fixture is the one ``test_instrument_archive.py``
already uses: twenty owner-approved cash symbols plus the Nifty 50 benchmark,
all twenty-one resolving unambiguously. Reusing it here, rather than a new
fixture, is deliberate -- it is the actual synthetic master this slice's
instrument-resolution wiring is required to prove itself against.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    DailyCandle,
    DailyHistoryBatch,
    DailyHistoryRequest,
)
from dhruva.contexts.marketdata.infrastructure.persistence.models import (
    DailyMarketBarRevisionModel,
)
from dhruva.contexts.marketdata.infrastructure.persistence.unit_of_work import (
    SqlAlchemyMarketDataUnitOfWork,
)
from dhruva.contexts.platform.application.identity.broker_credentials import (
    StoreBrokerCredential,
    StoreBrokerCredentialCommand,
)
from dhruva.contexts.platform.domain.broker.session import (
    BrokerApplication,
    BrokerSession,
    serialise_broker_application,
    serialise_broker_session,
)
from dhruva.contexts.platform.domain.identity.credentials import CredentialPurpose
from dhruva.contexts.platform.infrastructure.crypto import (
    MASTER_KEY_BYTES,
    MasterKeyProvider,
    seal_credential,
)
from dhruva.contexts.platform.infrastructure.database.identity_unit_of_work import (
    SqlAlchemyIdentityUnitOfWork,
)
from dhruva.contexts.reference.application.watchlist import ConfigureReferenceUniverse
from dhruva.contexts.reference.domain.instrument_master import InstrumentMasterSnapshot
from dhruva.contexts.reference.infrastructure import (
    ConfiguredNseCashCalendar,
    SqlAlchemyReferenceUnitOfWork,
    load_owner_universe,
)
from dhruva.contexts.reference.infrastructure.zerodha_instruments import parse_instrument_master
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.errors import UpstreamTimeoutError
from dhruva.shared.identity import AccountId, CredentialId, InstrumentId
from dhruva.shared.time.clock import FrozenClock
from dhruva.workers import marketdata as cli

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

ACCOUNT = AccountId.deterministic("owner-family")
BROKER = "zerodha"
#: Well before FETCHED_AT, so a coverage cutoff between the two sees the
#: watchlist but not yet the bars a refresh retrieves at FETCHED_AT.
WATCHLIST_RECORDED_AT = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
FETCHED_AT = datetime(2026, 8, 9, 6, 30, tzinfo=UTC)
AS_OF = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
#: The IST calendar date ``_ist_date(AS_OF)`` resolves to -- the market date a
#: refresh run at ``AS_OF`` asks the instrument-master source for.
MARKET_DATE = date(2026, 8, 10)
FIXTURE = Path(__file__).parents[1] / "fixtures" / "zerodha" / "instruments_sanitized.csv"

#: Fixed, obviously synthetic, and real key material -- the same shape
#: test_broker_authentication.py uses.
MASTER_KEY = SecretValue(base64.b64encode(bytes(range(MASTER_KEY_BYTES))).decode(), register=False)


def _factories(
    engine: AsyncEngine,
) -> tuple[
    Callable[[AccountId], SqlAlchemyReferenceUnitOfWork],
    Callable[[AccountId], SqlAlchemyMarketDataUnitOfWork],
    Callable[[AccountId], SqlAlchemyIdentityUnitOfWork],
]:
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
    return (
        lambda account: SqlAlchemyReferenceUnitOfWork(sessions, account_id=account),
        lambda account: SqlAlchemyMarketDataUnitOfWork(sessions, account_id=account),
        lambda account: SqlAlchemyIdentityUnitOfWork(sessions, account_id=account),
    )


async def _seed_watchlist(engine: AsyncEngine) -> None:
    reference_uow, _, _ = _factories(engine)
    await ConfigureReferenceUniverse(reference_uow).execute(
        load_owner_universe(ACCOUNT, recorded_at=WATCHLIST_RECORDED_AT)
    )


def _snapshot(market_date: date = MARKET_DATE) -> InstrumentMasterSnapshot:
    payload = FIXTURE.read_bytes()
    return InstrumentMasterSnapshot(
        provider="zerodha",
        market_date=market_date,
        fetched_at=FETCHED_AT,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        raw_csv=payload,
        entries=parse_instrument_master(payload),
    )


class FakeInstrumentSource:
    """Return one arranged provider snapshot without any network access."""

    def __init__(self, snapshot: InstrumentMasterSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    async def fetch(self, *, market_date: date) -> InstrumentMasterSnapshot:
        """Serve the arranged snapshot, refusing an unexpected market date."""
        self.calls += 1
        assert market_date == self.snapshot.market_date
        return self.snapshot


def _candle(trading_date: date) -> DailyCandle:
    price = Decimal("100")
    return DailyCandle(
        trading_date=trading_date,
        open=price,
        high=price + 1,
        low=price - 1,
        close=price,
        volume=1_000,
        open_interest=None,
    )


class FakeHistorySource:
    """Answer any bounded request with one candle per configured session.

    Every instrument normally gets the identical set of session dates, so the
    synchronized-session invariant holds by construction: this fake is testing
    the CLI's wiring and its atomic-refusal behavior without fabricating weekend
    candles a real daily-history provider would never return.
    """

    def __init__(
        self,
        *,
        retrieved_at: datetime,
        fail_for: InstrumentId | None = None,
        calendar: ConfiguredNseCashCalendar | None = None,
        omitted_dates: dict[InstrumentId, frozenset[date]] | None = None,
    ) -> None:
        self.retrieved_at = retrieved_at
        self.fail_for = fail_for
        self.calendar = calendar or ConfiguredNseCashCalendar()
        self.omitted_dates = omitted_dates or {}
        self.requests: list[DailyHistoryRequest] = []

    async def fetch(self, request: DailyHistoryRequest) -> DailyHistoryBatch:
        """Build one synthetic batch, or refuse for the arranged offender."""
        self.requests.append(request)
        if self.fail_for is not None and request.instrument_id == self.fail_for:
            raise UpstreamTimeoutError(
                "synthetic provider timeout", instrument_id=str(request.instrument_id)
            )
        dates: list[date] = []
        current = request.from_date
        while current <= request.to_date:
            if self.calendar.is_session(current) and current not in self.omitted_dates.get(
                request.instrument_id, frozenset()
            ):
                dates.append(current)
            current += timedelta(days=1)
        candles = tuple(_candle(item) for item in dates)
        raw = f"synthetic-{request.instrument_id}-{request.from_date}-{request.to_date}".encode()
        return DailyHistoryBatch(
            provider="zerodha",
            request=request,
            retrieved_at=self.retrieved_at,
            content_sha256=hashlib.sha256(raw).hexdigest(),
            raw_response=raw,
            adjustment_status=AdjustmentStatus.UNKNOWN,
            candles=candles,
        )


def _synthetic_application() -> BrokerApplication:
    """Return an obviously synthetic application credential -- never real material."""
    return BrokerApplication(
        identifier=SecretValue("synthetic-api-key", register=False),
        secret=SecretValue("synthetic-api-secret", register=False),
    )


async def _seal_session(
    engine: AsyncEngine,
    *,
    expires_at: datetime = AS_OF + timedelta(hours=6),
) -> None:
    """Seal an obviously synthetic ENROLMENT and SESSION credential pair."""
    _, _, identity_uow = _factories(engine)
    provider = MasterKeyProvider(MASTER_KEY)
    store = StoreBrokerCredential(identity_uow, provider, seal_credential)
    await store.execute(
        StoreBrokerCredentialCommand(
            account_id=ACCOUNT,
            broker=BROKER,
            purpose=CredentialPurpose.ENROLMENT,
            secret=serialise_broker_application(_synthetic_application()),
            at=FETCHED_AT,
            credential_id=CredentialId.new(),
        )
    )
    await store.execute(
        StoreBrokerCredentialCommand(
            account_id=ACCOUNT,
            broker=BROKER,
            purpose=CredentialPurpose.SESSION,
            secret=serialise_broker_session(
                BrokerSession(
                    token=SecretValue("synthetic-access-token", register=False),
                    broker_user_id="XX0000",
                    issued_at=FETCHED_AT,
                    expires_at=expires_at,
                )
            ),
            at=FETCHED_AT,
            credential_id=CredentialId.new(),
        )
    )


async def _bar_count(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        count = await connection.scalar(
            select(func.count()).select_from(DailyMarketBarRevisionModel)
        )
    return int(count or 0)


def _set_master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DHRUVA_CRYPTO__MASTER_KEY", MASTER_KEY.reveal())


# --------------------------------------------------------------------------- #
# Coverage: read-only, no network, exact aggregates
# --------------------------------------------------------------------------- #


async def test_coverage_reports_an_empty_watchlist_with_zero_aggregate(
    migrated: AsyncEngine,  # noqa: ARG001 - requests the migrated schema
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No watchlist means an empty, exact, complete report -- not an error."""
    _set_master_key(monkeypatch)

    exit_code = await cli.run(["coverage", "--account", str(ACCOUNT), "--as-of", AS_OF.isoformat()])

    assert exit_code == cli._EXIT_OK


async def test_coverage_reports_missing_mapping_before_any_archive_exists(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A seeded watchlist with no resolved mapping is MISSING_MAPPING, not NO_DATA."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)

    exit_code = await cli.run(["coverage", "--account", str(ACCOUNT), "--as-of", AS_OF.isoformat()])

    rendered = capsys.readouterr().out
    assert exit_code == cli._EXIT_OK
    assert "watchlist=20" in rendered
    assert "unmapped=20" in rendered
    assert "MISSING_MAPPING" in rendered


async def test_coverage_shows_ready_after_a_full_synchronized_refresh(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """After a successful bounded refresh, coverage reads READY for the whole watchlist."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    instrument_source = FakeInstrumentSource(_snapshot())
    history_source = FakeHistorySource(retrieved_at=FETCHED_AT)

    exit_code = await cli.run(
        ["refresh", "--account", str(ACCOUNT), "--as-of", AS_OF.isoformat()],
        instrument_source=instrument_source,
        history_source=history_source,
    )
    capsys.readouterr()
    coverage_exit = await cli.run(
        ["coverage", "--account", str(ACCOUNT), "--as-of", AS_OF.isoformat()]
    )
    rendered = capsys.readouterr().out

    assert exit_code == cli._EXIT_OK
    assert coverage_exit == cli._EXIT_OK
    assert "watchlist=20" in rendered
    assert "enough_history=20" in rendered
    assert "insufficient=0" in rendered
    assert instrument_source.calls == 1


# --------------------------------------------------------------------------- #
# Refresh: the SESSION gate, explicit and network-free
# --------------------------------------------------------------------------- #


async def test_refresh_refuses_with_nothing_enrolled(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No enrolment at all refuses before any adapter is built, naming the remedy."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)

    exit_code = await cli.run(["refresh", "--account", str(ACCOUNT), "--as-of", AS_OF.isoformat()])

    assert exit_code == cli._EXIT_NOT_AUTHENTICATED
    assert "enrol" in capsys.readouterr().err.lower()


async def test_refresh_refuses_when_never_logged_in(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SESSION_MISSING is reported explicitly, distinct from ENROLMENT_MISSING."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    _, _, identity_uow = _factories(migrated)
    provider = MasterKeyProvider(MASTER_KEY)
    await StoreBrokerCredential(identity_uow, provider, seal_credential).execute(
        StoreBrokerCredentialCommand(
            account_id=ACCOUNT,
            broker=BROKER,
            purpose=CredentialPurpose.ENROLMENT,
            secret=serialise_broker_application(_synthetic_application()),
            at=FETCHED_AT,
            credential_id=CredentialId.new(),
        )
    )

    exit_code = await cli.run(
        ["refresh", "--account", str(ACCOUNT), "--as-of", AS_OF.isoformat()],
        clock=FrozenClock(AS_OF),
    )

    assert exit_code == cli._EXIT_NOT_AUTHENTICATED
    assert "SESSION_MISSING" in capsys.readouterr().err


async def test_refresh_refuses_when_the_session_has_expired(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """SESSION_EXPIRED, not a confusing provider error three calls later."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    await _seal_session(migrated, expires_at=AS_OF - timedelta(hours=1))

    exit_code = await cli.run(
        ["refresh", "--account", str(ACCOUNT), "--as-of", AS_OF.isoformat()],
        clock=FrozenClock(AS_OF),
    )

    assert exit_code == cli._EXIT_NOT_AUTHENTICATED
    assert "SESSION_EXPIRED" in capsys.readouterr().err


async def test_refresh_never_checks_the_session_when_nothing_is_missing(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Adequate stored history means zero provider calls -- and no SESSION demand either."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    instrument_source = FakeInstrumentSource(_snapshot())
    history_source = FakeHistorySource(retrieved_at=FETCHED_AT)
    await cli.run(
        ["refresh", "--account", str(ACCOUNT), "--as-of", AS_OF.isoformat()],
        instrument_source=instrument_source,
        history_source=history_source,
    )
    capsys.readouterr()

    # No credential of any kind is enrolled at this point, and no fake source
    # is injected this time -- if the session were checked, this would refuse.
    exit_code = await cli.run(["refresh", "--account", str(ACCOUNT), "--as-of", AS_OF.isoformat()])

    assert exit_code == cli._EXIT_OK
    assert "no provider call was made" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Refresh: bounded ingest, atomic refusal, idempotency
# --------------------------------------------------------------------------- #


async def test_refresh_ingests_within_the_configured_bootstrap_bound(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fetched range never exceeds the twenty-calendar-day bootstrap window."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    instrument_source = FakeInstrumentSource(_snapshot())
    history_source = FakeHistorySource(retrieved_at=FETCHED_AT)

    exit_code = await cli.run(
        ["refresh", "--account", str(ACCOUNT), "--as-of", AS_OF.isoformat()],
        instrument_source=instrument_source,
        history_source=history_source,
    )

    assert exit_code == cli._EXIT_OK
    spans = {(request.from_date, request.to_date) for request in history_source.requests}
    assert len(spans) == 1
    (from_date, to_date) = next(iter(spans))
    assert (to_date - from_date).days == 19
    assert to_date == AS_OF.date()
    assert len(history_source.requests) == 21  # twenty watchlist symbols plus the benchmark


@pytest.mark.parametrize(
    ("cutoff", "expected_through"),
    [
        (datetime(2026, 8, 15, 12, 0, tzinfo=UTC), date(2026, 8, 14)),
        (datetime(2026, 8, 16, 12, 0, tzinfo=UTC), date(2026, 8, 14)),
        (datetime(2026, 8, 17, 9, 59, tzinfo=UTC), date(2026, 8, 14)),
        (datetime(2026, 8, 18, 12, 0, tzinfo=UTC), date(2026, 8, 18)),
    ],
)
async def test_refresh_uses_one_latest_completed_session_for_the_whole_batch(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    cutoff: datetime,
    expected_through: date,
) -> None:
    """Saturday, Sunday and pre-close Monday all share the correct batch endpoint."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    calendar = ConfiguredNseCashCalendar()
    instrument_source = FakeInstrumentSource(_snapshot(market_date=cli._ist_date(cutoff)))
    history_source = FakeHistorySource(
        retrieved_at=cutoff - timedelta(minutes=1), calendar=calendar
    )

    exit_code = await cli.run(
        ["refresh", "--account", str(ACCOUNT), "--as-of", cutoff.isoformat()],
        instrument_source=instrument_source,
        history_source=history_source,
        trading_calendar=calendar,
    )

    assert exit_code == cli._EXIT_OK
    assert len(history_source.requests) == 21
    assert {request.to_date for request in history_source.requests} == {expected_through}


async def test_missing_expected_friday_refuses_before_any_bar_is_persisted(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Weekend awareness does not excuse an actually expected benchmark session."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    cutoff = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    expected_friday = date(2026, 8, 14)
    benchmark_id = InstrumentId.deterministic("reference", "nse-index-nifty-50")
    history_source = FakeHistorySource(
        retrieved_at=cutoff - timedelta(minutes=1),
        omitted_dates={benchmark_id: frozenset({expected_friday})},
    )

    exit_code = await cli.run(
        ["refresh", "--account", str(ACCOUNT), "--as-of", cutoff.isoformat()],
        instrument_source=FakeInstrumentSource(_snapshot(market_date=date(2026, 8, 16))),
        history_source=history_source,
    )

    rendered = capsys.readouterr().err
    assert exit_code == cli._EXIT_REFUSED
    assert "benchmark daily history is stale or incomplete" in rendered
    assert "required_through: 2026-08-14" in rendered
    assert await _bar_count(migrated) == 0


async def test_monday_close_transitions_friday_coverage_from_ready_to_refreshable(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Friday is current before Monday close, then Monday becomes exactly required."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    sunday = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    await cli.run(
        ["refresh", "--account", str(ACCOUNT), "--as-of", sunday.isoformat()],
        instrument_source=FakeInstrumentSource(_snapshot(market_date=date(2026, 8, 16))),
        history_source=FakeHistorySource(retrieved_at=sunday - timedelta(minutes=1)),
    )
    capsys.readouterr()

    monday_before_close = datetime(2026, 8, 17, 9, 59, tzinfo=UTC)
    preclose_instruments = FakeInstrumentSource(_snapshot(market_date=date(2026, 8, 17)))
    preclose_history = FakeHistorySource(retrieved_at=monday_before_close)
    preclose_exit = await cli.run(
        [
            "refresh",
            "--account",
            str(ACCOUNT),
            "--as-of",
            monday_before_close.isoformat(),
        ],
        instrument_source=preclose_instruments,
        history_source=preclose_history,
    )
    capsys.readouterr()

    monday_after_close = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    postclose_history = FakeHistorySource(retrieved_at=monday_after_close)
    postclose_exit = await cli.run(
        [
            "refresh",
            "--account",
            str(ACCOUNT),
            "--as-of",
            monday_after_close.isoformat(),
        ],
        instrument_source=FakeInstrumentSource(_snapshot(market_date=date(2026, 8, 17))),
        history_source=postclose_history,
    )

    assert preclose_exit == cli._EXIT_OK
    assert preclose_instruments.calls == 0
    assert preclose_history.requests == []
    assert postclose_exit == cli._EXIT_OK
    assert {request.to_date for request in postclose_history.requests} == {date(2026, 8, 17)}


async def test_a_provider_timeout_on_one_instrument_refuses_the_whole_batch(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Whole-run refusal: zero rows persist when one instrument's fetch fails."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    instrument_source = FakeInstrumentSource(_snapshot())
    offender = InstrumentId.deterministic("reference", "nse-equity-canara-bank")
    history_source = FakeHistorySource(retrieved_at=FETCHED_AT, fail_for=offender)

    exit_code = await cli.run(
        ["refresh", "--account", str(ACCOUNT), "--as-of", AS_OF.isoformat()],
        instrument_source=instrument_source,
        history_source=history_source,
    )

    assert exit_code == cli._EXIT_REFUSED
    assert "REFUSED" in capsys.readouterr().err
    assert await _bar_count(migrated) == 0


async def test_a_repeated_refresh_is_idempotent(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Running refresh twice with identical evidence adds nothing the second time."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    instrument_source = FakeInstrumentSource(_snapshot())
    history_source = FakeHistorySource(retrieved_at=FETCHED_AT)
    await cli.run(
        ["refresh", "--account", str(ACCOUNT), "--as-of", AS_OF.isoformat()],
        instrument_source=instrument_source,
        history_source=history_source,
    )
    capsys.readouterr()
    first_count = await _bar_count(migrated)

    second_history_source = FakeHistorySource(retrieved_at=FETCHED_AT)
    exit_code = await cli.run(
        ["refresh", "--account", str(ACCOUNT), "--as-of", AS_OF.isoformat()],
        instrument_source=FakeInstrumentSource(_snapshot()),
        history_source=second_history_source,
    )

    assert exit_code == cli._EXIT_OK
    assert await _bar_count(migrated) == first_count
    # Coverage was already sufficient, so the second run made no request at all.
    assert second_history_source.requests == []


async def test_a_future_observed_bar_does_not_leak_into_an_earlier_coverage_read(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Point-in-time holds for refresh's own writes, not only for reads written by hand."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    instrument_source = FakeInstrumentSource(_snapshot())
    history_source = FakeHistorySource(retrieved_at=FETCHED_AT)
    await cli.run(
        ["refresh", "--account", str(ACCOUNT), "--as-of", AS_OF.isoformat()],
        instrument_source=instrument_source,
        history_source=history_source,
    )
    capsys.readouterr()

    earlier_cutoff = FETCHED_AT - timedelta(minutes=1)
    exit_code = await cli.run(
        ["coverage", "--account", str(ACCOUNT), "--as-of", earlier_cutoff.isoformat()]
    )
    rendered = capsys.readouterr().out

    assert exit_code == cli._EXIT_OK
    assert "no_data=20" in rendered
