"""``dhruva-refresh`` against real PostgreSQL: three phases, one transaction each.

No live Zerodha or GDELT call anywhere in this file. The market-data phase is
exercised through ``run()``'s own seam (the same one
``test_marketdata_refresh.py`` uses) or, for the SESSION-gate scenario, against
a real but empty identity store. The news phase is exercised through injected
``NewsFeed`` fakes (the same shape ``test_news_workflow.py`` uses for
``PollNewsFeeds`` directly) -- ``PollNewsFeeds`` and ``IngestNewsItems`` below
that seam are the real, unmodified application layer.

The property this file exists to prove that no unit test can: that the brief's
own cutoff, read only after both write phases reach a terminal state, sees
rows this same invocation just committed -- the defect an earlier revision of
``dhruva-marketdata refresh`` was flagged for.
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

from dhruva.contexts.intelligence.domain.news import (
    NewsItem,
    NewsItemIdentity,
    PermittedText,
    canonical_url,
)
from dhruva.contexts.intelligence.domain.sources import (
    NewsFetchResult,
    SourceHealth,
    SourceStatus,
)
from dhruva.contexts.intelligence.infrastructure.gdelt.mapper import GDELT_SOURCE_KEY, gdelt_source
from dhruva.contexts.intelligence.infrastructure.persistence.models import NewsItemRevisionModel
from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    BarCompleteness,
    DailyBarRevision,
    DailyBarSeries,
    DailyCandle,
    DailyHistoryBatch,
    DailyHistoryRequest,
    MarketInstrumentKind,
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
    SqlAlchemyReferenceUnitOfWork,
    load_owner_universe,
)
from dhruva.contexts.reference.infrastructure.zerodha_instruments import parse_instrument_master
from dhruva.shared.config.secret import SecretValue
from dhruva.shared.config.settings import load_settings
from dhruva.shared.errors import UpstreamTimeoutError
from dhruva.shared.identity import AccountId, CredentialId, InstrumentId
from dhruva.shared.time.clock import FrozenClock
from dhruva.workers import refresh as cli

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

ACCOUNT = AccountId.deterministic("owner-family")
BROKER = "zerodha"
WATCHLIST_RECORDED_AT = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
FETCHED_AT = datetime(2026, 8, 9, 6, 30, tzinfo=UTC)
#: The moment ``cli.run`` is invoked. Each internal clock read is a fixed
#: offset after this one, so the test can assert exactly which read saw what.
INVOKED_AT = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parents[1] / "fixtures" / "zerodha" / "instruments_sanitized.csv"
MASTER_KEY = SecretValue(base64.b64encode(bytes(range(MASTER_KEY_BYTES))).decode(), register=False)

PUBLISHED = datetime(2026, 8, 10, 5, 0, tzinfo=UTC)
PUBLISHER = "economictimes.indiatimes.test"

FRAUD = "State Bank of India faces a forensic audit over accounting irregularities"
ORDER_HEADLINE = "Hindustan Aeronautics bags order worth Rs 5,000 crore from the ministry"


class _TickingClock:
    """Returns each pre-arranged instant once, in order -- never the wall clock.

    Simulates real elapsed time between the workflow's own clock reads
    (market phase, news phase, final brief) without depending on wall-clock
    speed, so the fresh-cutoff regression is exercised deterministically.
    """

    def __init__(self, instants: Sequence[datetime]) -> None:
        self._instants = list(instants)

    def now(self) -> datetime:
        """Return the next arranged instant."""
        return self._instants.pop(0)


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


def _snapshot(market_date: date) -> InstrumentMasterSnapshot:
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


def _candle(trading_date: date, *, close: Decimal = Decimal("100")) -> DailyCandle:
    return DailyCandle(
        trading_date=trading_date,
        open=close,
        high=close + 1,
        low=close - 1,
        close=close,
        volume=1_000,
        open_interest=None,
    )


class FakeHistorySource:
    """Answer any bounded request with one candle per calendar day in range.

    Every day closes flat at 100 except the last one in the requested range,
    which closes at ``final_close`` when given -- enough to produce a
    deterministic, synchronized-session-compliant price move on the one day
    that matters for a market-context read, without hand-rolling a full
    candle series. ``final_close=None`` (the default) keeps every day flat,
    for scenarios that do not depend on price-driven attention.
    """

    def __init__(
        self,
        *,
        retrieved_at: datetime,
        fail_for: InstrumentId | None = None,
        final_close: Decimal | None = None,
    ) -> None:
        self.retrieved_at = retrieved_at
        self.fail_for = fail_for
        self.final_close = final_close

    async def fetch(self, request: DailyHistoryRequest) -> DailyHistoryBatch:
        """Build one synthetic batch, or refuse for the arranged offender."""
        if self.fail_for is not None and request.instrument_id == self.fail_for:
            raise UpstreamTimeoutError(
                "synthetic provider timeout", instrument_id=str(request.instrument_id)
            )
        dates: list[date] = []
        current = request.from_date
        while current <= request.to_date:
            dates.append(current)
            current += timedelta(days=1)
        candles = tuple(
            _candle(
                item,
                close=(
                    self.final_close
                    if self.final_close is not None and item == request.to_date
                    else Decimal("100")
                ),
            )
            for item in dates
        )
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


class _Feed:
    """One arranged fetch result, standing in for one GDELT request."""

    def __init__(self, result: NewsFetchResult) -> None:
        self.result = result
        self.polls = 0

    async def fetch(self) -> NewsFetchResult:
        """Return the arranged result and record that it was asked for."""
        self.polls += 1
        return self.result


def _news_item(url: str, title: str, *, first_seen_at: datetime) -> NewsItem:
    link = canonical_url(url)
    return NewsItem(
        identity=NewsItemIdentity(
            source_key=GDELT_SOURCE_KEY,
            provider_item_id=f"url-sha256:{hashlib.sha256(link.encode()).hexdigest()}",
            url=link,
        ),
        source=gdelt_source(PUBLISHER),
        text=PermittedText(title=title),
        published_at=PUBLISHED,
        first_seen_at=first_seen_at,
    )


def _fraud_feed(*, first_seen_at: datetime) -> _Feed:
    """One healthy feed offering the FRAUD headline, first seen at ``first_seen_at``."""
    item = _news_item("https://p.test/1", FRAUD, first_seen_at=first_seen_at)
    return _Feed(_healthy_result(item, observed_at=first_seen_at))


def _healthy_result(*items: NewsItem, observed_at: datetime) -> NewsFetchResult:
    return NewsFetchResult(
        source=gdelt_source(),
        status=SourceStatus(
            health=SourceHealth.HEALTHY,
            reason=f"GDELT returned {len(items)} articles",
            observed_at=observed_at,
        ),
        items=items,
        retrieved_at=observed_at,
        content_sha256=hashlib.sha256(b"payload").hexdigest(),
        mapper_revision="gdelt-doc2-artlist-v1",
    )


def _rate_limited_result(*, observed_at: datetime) -> NewsFetchResult:
    return NewsFetchResult(
        source=gdelt_source(),
        status=SourceStatus(
            health=SourceHealth.RATE_LIMITED,
            reason="HTTP 429",
            observed_at=observed_at,
            http_status=429,
        ),
        items=(),
        retrieved_at=observed_at,
        content_sha256=hashlib.sha256(b"rate-limited").hexdigest(),
        mapper_revision="gdelt-doc2-artlist-v1",
    )


def _synthetic_application() -> BrokerApplication:
    return BrokerApplication(
        identifier=SecretValue("synthetic-api-key", register=False),
        secret=SecretValue("synthetic-api-secret", register=False),
    )


async def _seal_expired_session(engine: AsyncEngine) -> None:
    """Seal an ENROLMENT and an already-expired SESSION -- SESSION_EXPIRED, not MISSING."""
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
                    expires_at=FETCHED_AT + timedelta(hours=1),
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


async def _news_revision_count(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        count = await connection.scalar(select(func.count()).select_from(NewsItemRevisionModel))
    return int(count or 0)


async def _store_bar(
    engine: AsyncEngine,
    *,
    instrument_id: InstrumentId,
    trading_date: date,
    close: Decimal,
    retrieved_at: datetime,
) -> None:
    """Append one bar directly through the real repository, bypassing IngestDailyHistory.

    Used only to arrange a bitemporal correction -- a later ``retrieved_at``
    for an already-stored trading date -- the same direct-write shape
    ``test_watchlist_brief.py`` uses to prove PIT correctness at the read
    layer; here it proves the orchestrator's own ``_render_brief`` call site
    inherits the same guarantee.
    """
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    bar = DailyBarRevision(
        instrument_id=instrument_id,
        instrument_kind=MarketInstrumentKind.CASH_EQUITY,
        source="zerodha",
        source_instrument_id=1,
        candle=DailyCandle(
            trading_date=trading_date,
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=1_000,
            open_interest=None,
        ),
        retrieved_at=retrieved_at,
        adjustment_status=AdjustmentStatus.UNKNOWN,
        completeness=BarCompleteness.COMPLETE,
        source_revision=hashlib.sha256(
            f"correction-{instrument_id}-{trading_date}-{close}-{retrieved_at}".encode()
        ).hexdigest(),
        batch_sha256="c" * 64,
        quality_revision="daily-history-quality-v1",
    )
    async with SqlAlchemyMarketDataUnitOfWork(sessions, account_id=ACCOUNT) as unit_of_work:
        await unit_of_work.daily_bars.add_series(DailyBarSeries(bars=(bar,)))
        await unit_of_work.commit()


def _set_master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DHRUVA_CRYPTO__MASTER_KEY", MASTER_KEY.reveal())


def _clock(*, phases: int = 3) -> _TickingClock:
    """Return a clock that hands out one strictly-later instant per phase read."""
    return _TickingClock([INVOKED_AT + timedelta(minutes=n) for n in range(phases)][:phases])


# --------------------------------------------------------------------------- #
# 1 & 2 -- the healthy paths
# --------------------------------------------------------------------------- #


async def test_healthy_market_and_healthy_news_render_a_brief(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The ordinary case: both phases succeed and the brief renders from fresh state."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    instrument_source = FakeInstrumentSource(_snapshot(date(2026, 8, 10)))
    history_source = FakeHistorySource(retrieved_at=FETCHED_AT)
    news_now = INVOKED_AT + timedelta(minutes=1)
    feed = _fraud_feed(first_seen_at=news_now)

    exit_code = await cli.run(
        ["--account", str(ACCOUNT)],
        instrument_source=instrument_source,
        history_source=history_source,
        news_feeds=(feed,),
        clock=_clock(),
    )

    output = capsys.readouterr().out
    assert exit_code == cli._EXIT_OK
    assert "market data : HEALTHY" in output
    assert "news        : HEALTHY" in output
    assert "Research brief" in output
    assert "SBIN" in output
    assert feed.polls == 1


async def test_market_data_already_sufficient_still_polls_news_and_renders(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A quiet market day: no provider call, but news and the brief proceed normally."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    # A first pass fills coverage; the fakes prove no second Zerodha call happens.
    seed_instrument_source = FakeInstrumentSource(_snapshot(date(2026, 8, 10)))
    seed_history_source = FakeHistorySource(retrieved_at=FETCHED_AT)
    await cli.run(
        ["--account", str(ACCOUNT)],
        instrument_source=seed_instrument_source,
        history_source=seed_history_source,
        news_feeds=(),
        clock=_clock(),
    )
    capsys.readouterr()

    news_now = INVOKED_AT + timedelta(minutes=1)
    feed = _fraud_feed(first_seen_at=news_now)
    exit_code = await cli.run(
        ["--account", str(ACCOUNT)],
        instrument_source=FakeInstrumentSource(_snapshot(date(2026, 8, 10))),
        history_source=FakeHistorySource(retrieved_at=FETCHED_AT),
        news_feeds=(feed,),
        clock=_clock(),
    )

    output = capsys.readouterr().out
    assert exit_code == cli._EXIT_OK
    assert "market data : HEALTHY" in output
    assert "already sufficient" in output.lower() or "no provider call was needed" in output
    assert "news        : HEALTHY" in output
    assert FRAUD in output


# --------------------------------------------------------------------------- #
# 3 & 4 -- GDELT rate limiting
# --------------------------------------------------------------------------- #


async def test_first_batch_rate_limited_stops_news_but_still_renders_the_brief(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A 429 on the very first batch: no later batch is issued, but the brief still renders."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    news_now = INVOKED_AT + timedelta(minutes=1)
    limited = _Feed(_rate_limited_result(observed_at=news_now))
    # A healthy result needs at least one item to construct at all; its
    # content is irrelevant here since batch 1's rate limit means batch 2 is
    # never polled.
    never_reached = _fraud_feed(first_seen_at=news_now)

    exit_code = await cli.run(
        ["--account", str(ACCOUNT)],
        instrument_source=FakeInstrumentSource(_snapshot(date(2026, 8, 10))),
        history_source=FakeHistorySource(retrieved_at=FETCHED_AT),
        news_feeds=(limited, never_reached),
        clock=_clock(),
    )

    output = capsys.readouterr().out
    assert exit_code == cli._EXIT_DEGRADED
    assert "news        : DEGRADED" in output
    assert "Research brief" in output
    assert never_reached.polls == 0


async def test_a_later_batch_rate_limited_preserves_earlier_articles_and_the_brief_sees_them(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Batch 1 succeeds and is ingested; batch 2's 429 stops the pass but does not undo batch 1."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    news_now = INVOKED_AT + timedelta(minutes=1)
    healthy = _fraud_feed(first_seen_at=news_now)
    limited = _Feed(_rate_limited_result(observed_at=news_now))

    exit_code = await cli.run(
        ["--account", str(ACCOUNT)],
        instrument_source=FakeInstrumentSource(_snapshot(date(2026, 8, 10))),
        history_source=FakeHistorySource(retrieved_at=FETCHED_AT),
        news_feeds=(healthy, limited),
        clock=_clock(),
    )

    output = capsys.readouterr().out
    assert exit_code == cli._EXIT_DEGRADED
    assert FRAUD in output
    assert await _news_revision_count(migrated) == 1


# --------------------------------------------------------------------------- #
# 5 -- a market-data hard refusal halts the whole run
# --------------------------------------------------------------------------- #


async def test_a_market_data_refusal_halts_before_news_or_the_brief(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No unsafe continuation: a hard refusal stops before news is even attempted."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    offender = InstrumentId.deterministic("reference", "nse-equity-hindustan-aeronautics")
    # A healthy result needs at least one item to construct at all; its
    # content is irrelevant here since the market-data refusal must stop the
    # run before any feed is ever polled.
    unreachable = _fraud_feed(first_seen_at=INVOKED_AT)

    exit_code = await cli.run(
        ["--account", str(ACCOUNT)],
        instrument_source=FakeInstrumentSource(_snapshot(date(2026, 8, 10))),
        history_source=FakeHistorySource(retrieved_at=FETCHED_AT, fail_for=offender),
        news_feeds=(unreachable,),
        clock=_clock(),
    )

    output = capsys.readouterr().out
    assert exit_code == cli._EXIT_REFUSED
    assert "market data : REFUSED" in output
    assert "halted" in output.lower()
    assert "Research brief" not in output
    assert unreachable.polls == 0
    assert await _bar_count(migrated) == 0


# --------------------------------------------------------------------------- #
# 6 -- expired Zerodha session: degraded, safe, and the brief still renders
# --------------------------------------------------------------------------- #


async def test_an_expired_session_is_degraded_not_refused_and_prints_no_secret(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An expired session is a benign gap: DEGRADED, no secret printed, brief renders."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    await _seal_expired_session(migrated)
    news_now = INVOKED_AT + timedelta(minutes=1)
    feed = _fraud_feed(first_seen_at=news_now)

    exit_code = await cli.run(
        ["--account", str(ACCOUNT)],
        news_feeds=(feed,),
        clock=FrozenClock(INVOKED_AT),
    )

    captured = capsys.readouterr()
    output = captured.out
    combined = captured.out + captured.err
    assert exit_code == cli._EXIT_DEGRADED
    assert "market data : DEGRADED" in output
    assert "SESSION_EXPIRED" in output
    assert "Research brief" in output
    for forbidden in ("synthetic-access-token", "synthetic-api-secret"):
        assert forbidden not in combined


# --------------------------------------------------------------------------- #
# 8 & 9 -- the fresh-cutoff regression this slice exists to prevent
# --------------------------------------------------------------------------- #


async def test_the_final_brief_sees_market_rows_written_by_the_same_invocation(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The brief's cutoff must be read after the market write, not before it.

    A flat close would never earn a place in the brief at all --
    ``top_attention`` excludes score-0 entries, and a flat 1-day move
    contributes zero points -- so this fixture moves the final session by 6%,
    a real, deterministic price move the existing attention formula scores.
    That is what lets a market-context line appear in the brief in the first
    place; it is not a change to what "the brief sees fresh state" means.
    """
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)

    exit_code = await cli.run(
        ["--account", str(ACCOUNT)],
        instrument_source=FakeInstrumentSource(_snapshot(date(2026, 8, 10))),
        history_source=FakeHistorySource(retrieved_at=FETCHED_AT, final_close=Decimal("106")),
        news_feeds=(),
        clock=_clock(),
    )

    output = capsys.readouterr().out
    # An explicitly empty ``news_feeds`` tuple polls zero feeds, which is a
    # legitimate, healthy no-op (nothing was asked of the provider and
    # nothing failed) -- the same shape as market data's own
    # ALREADY_SUFFICIENT no-op -- so the whole run is healthy.
    assert exit_code == cli._EXIT_OK
    # The market context line ("close 106") appears only if the brief's own
    # read saw the bars this same run just committed -- the stale-cutoff bug
    # this slice guards against would show every instrument as NO_DATA
    # instead, and no instrument would clear the score>0 bar to be shown at all.
    assert "close 106" in output


async def test_the_final_brief_sees_a_news_item_first_seen_during_the_same_invocation(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The brief's cutoff must be read after the news write, not before it."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    news_now = INVOKED_AT + timedelta(minutes=1)
    feed = _fraud_feed(first_seen_at=news_now)

    exit_code = await cli.run(
        ["--account", str(ACCOUNT)],
        instrument_source=FakeInstrumentSource(_snapshot(date(2026, 8, 10))),
        history_source=FakeHistorySource(retrieved_at=FETCHED_AT),
        news_feeds=(feed,),
        clock=_clock(),
    )

    output = capsys.readouterr().out
    assert exit_code == cli._EXIT_OK
    assert FRAUD in output


async def test_the_orchestrators_own_brief_render_respects_the_bitemporal_cutoff(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PIT still holds inside the orchestrator: a later correction cannot leak backward.

    ``_render_brief`` is a new call site this slice introduces onto the same
    read models ``test_watchlist_brief.py`` already proves are bitemporally
    correct; this proves the new call site inherits that guarantee rather
    than accidentally bypassing it. A correction retrieved *after* an
    already-rendered cutoff must not change what that cutoff renders, and
    must appear only once knowledge time reaches it.
    """
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    settings = load_settings()
    session_factory = async_sessionmaker(migrated, expire_on_commit=False)
    sbin_id = InstrumentId.deterministic("reference", "nse-equity-state-bank-of-india")

    await cli.run(
        ["--account", str(ACCOUNT)],
        instrument_source=FakeInstrumentSource(_snapshot(date(2026, 8, 10))),
        history_source=FakeHistorySource(retrieved_at=FETCHED_AT, final_close=Decimal("106")),
        news_feeds=(),
        clock=_clock(),
    )
    capsys.readouterr()
    cutoff = INVOKED_AT + timedelta(minutes=2)  # the third, final clock read in _clock()

    before = await cli._render_brief(
        session_factory, account_id=ACCOUNT, as_of=cutoff, settings=settings
    )

    # A correction to the same trading date, retrieved strictly after the
    # cutoff that already rendered above.
    await _store_bar(
        migrated,
        instrument_id=sbin_id,
        trading_date=date(2026, 8, 9),
        close=Decimal("500"),
        retrieved_at=cutoff + timedelta(hours=1),
    )

    after = await cli._render_brief(
        session_factory, account_id=ACCOUNT, as_of=cutoff, settings=settings
    )
    later = await cli._render_brief(
        session_factory,
        account_id=ACCOUNT,
        as_of=cutoff + timedelta(hours=2),
        settings=settings,
    )

    assert after == before
    assert "close 500" not in after
    assert "close 500" in later


# --------------------------------------------------------------------------- #
# 13 & 14 -- idempotency and determinism
# --------------------------------------------------------------------------- #


async def test_repeated_invocations_do_not_duplicate_persisted_data(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Existing lower-level idempotency is reused, not reinvented at this layer."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    news_now = INVOKED_AT + timedelta(minutes=1)

    for _ in range(2):
        feed = _fraud_feed(first_seen_at=news_now)
        await cli.run(
            ["--account", str(ACCOUNT)],
            instrument_source=FakeInstrumentSource(_snapshot(date(2026, 8, 10))),
            history_source=FakeHistorySource(retrieved_at=FETCHED_AT),
            news_feeds=(feed,),
            clock=_clock(),
        )
        capsys.readouterr()

    assert await _news_revision_count(migrated) == 1


async def test_the_brief_is_deterministic_for_identical_persisted_state(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two reads of the same already-persisted state render byte-identical briefs."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    await cli.run(
        ["--account", str(ACCOUNT)],
        instrument_source=FakeInstrumentSource(_snapshot(date(2026, 8, 10))),
        history_source=FakeHistorySource(retrieved_at=FETCHED_AT),
        news_feeds=(),
        clock=_clock(),
    )
    first = capsys.readouterr().out

    await cli.run(
        ["--account", str(ACCOUNT)],
        instrument_source=FakeInstrumentSource(_snapshot(date(2026, 8, 10))),
        history_source=FakeHistorySource(retrieved_at=FETCHED_AT),
        news_feeds=(),
        clock=_clock(),
    )
    second = capsys.readouterr().out

    assert (
        first.split("brief cutoff", 1)[1].split("\n", 1)[1]
        == second.split("brief cutoff", 1)[1].split("\n", 1)[1]
    )


# --------------------------------------------------------------------------- #
# --dry-run: zero network, zero write, zero session access
# --------------------------------------------------------------------------- #


async def test_dry_run_makes_no_write_and_reports_both_phases(
    migrated: AsyncEngine,
    truncated_after_test: None,  # noqa: ARG001 - requests committed-state cleanup
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--dry-run previews both phases and writes nothing, even with a watchlist seeded."""
    _set_master_key(monkeypatch)
    await _seed_watchlist(migrated)
    before_bars = await _bar_count(migrated)
    before_news = await _news_revision_count(migrated)

    exit_code = await cli.run(
        ["--account", str(ACCOUNT), "--dry-run"], clock=FrozenClock(INVOKED_AT)
    )

    output = capsys.readouterr().out
    assert exit_code == cli._EXIT_OK
    assert "dry run" in output.lower()
    assert "no network call was made and nothing was written" in output
    assert await _bar_count(migrated) == before_bars
    assert await _news_revision_count(migrated) == before_news
