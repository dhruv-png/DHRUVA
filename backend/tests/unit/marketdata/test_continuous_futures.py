"""The continuous series is stitched deterministically and is research-only."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from dhruva.contexts.marketdata.domain.continuous_futures import (
    CONTINUOUS_FUTURES_POLICY_REVISION,
    ContinuousBar,
    ContinuousFuturesSeries,
    FuturesContractTerms,
    FuturesRoll,
    RollPolicy,
    RollReason,
    build_continuous_series,
)
from dhruva.contexts.marketdata.domain.daily_bars import (
    AdjustmentStatus,
    BarCompleteness,
    DailyBarRevision,
    DailyBarSeries,
    DailyCandle,
    MarketInstrumentKind,
)
from dhruva.shared.errors import MissingDataError, ValidationError
from dhruva.shared.identity import InstrumentId
from dhruva.shared.invariants import InvariantViolation

pytestmark = pytest.mark.unit

SESSIONS = tuple(date(2026, 7, 1) + timedelta(days=item) for item in range(12))
RETRIEVED = datetime(2026, 7, 13, 6, tzinfo=UTC)
UNDERLYING = InstrumentId.deterministic("reference", "nse-equity-sbin")
FRONT = InstrumentId.deterministic("reference", "nfo-fut-sbin-front")
NEXT = InstrumentId.deterministic("reference", "nfo-fut-sbin-next")
FAR = InstrumentId.deterministic("reference", "nfo-fut-sbin-far")

POLICY = RollPolicy(expiry_buffer_sessions=2)

FRONT_TERMS = FuturesContractTerms(
    contract_id=FRONT, instrument_token=111, expiry=SESSIONS[9], lot_size=750
)
NEXT_TERMS = FuturesContractTerms(
    contract_id=NEXT, instrument_token=222, expiry=SESSIONS[11], lot_size=1500
)


def _candle(
    trading_date: date,
    *,
    close: str = "100",
    volume: int = 1000,
    open_interest: int | None = 5000,
) -> DailyCandle:
    return DailyCandle(
        trading_date=trading_date,
        open=Decimal("99"),
        high=Decimal("105"),
        low=Decimal("98"),
        close=Decimal(close),
        volume=volume,
        open_interest=open_interest,
    )


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _series(
    instrument_id: InstrumentId,
    candles: tuple[DailyCandle, ...],
    *,
    token: int = 111,
    incomplete_from: date | None = None,
) -> DailyBarSeries:
    return DailyBarSeries(
        bars=tuple(
            DailyBarRevision(
                instrument_id=instrument_id,
                instrument_kind=MarketInstrumentKind.FUTURES_CONTRACT,
                source="zerodha",
                source_instrument_id=token,
                candle=candle,
                retrieved_at=RETRIEVED,
                adjustment_status=AdjustmentStatus.UNKNOWN,
                completeness=(
                    BarCompleteness.INCOMPLETE
                    if incomplete_from is not None and candle.trading_date >= incomplete_from
                    else BarCompleteness.COMPLETE
                ),
                source_revision=_digest(
                    str(instrument_id), candle.trading_date.isoformat(), "revision"
                ),
                batch_sha256=_digest(str(instrument_id), "batch"),
                quality_revision="daily-history-quality-v1",
            )
            for candle in candles
        )
    )


def _front_history(sessions: tuple[date, ...] = SESSIONS[:10]) -> DailyBarSeries:
    return _series(FRONT, tuple(_candle(item) for item in sessions), token=111)


def _next_history(
    *,
    overtakes_from: date | None = None,
    sessions: tuple[date, ...] = SESSIONS,
) -> DailyBarSeries:
    return _series(
        NEXT,
        tuple(
            _candle(
                item,
                close="102",
                volume=2000 if overtakes_from is not None and item >= overtakes_from else 100,
                open_interest=9000
                if overtakes_from is not None and item >= overtakes_from
                else 400,
            )
            for item in sessions
        ),
        token=222,
    )


def _build(
    *,
    terms: tuple[FuturesContractTerms, ...] = (FRONT_TERMS, NEXT_TERMS),
    histories: dict[InstrumentId, DailyBarSeries] | None = None,
    sessions: tuple[date, ...] = SESSIONS,
    policy: RollPolicy = POLICY,
) -> ContinuousFuturesSeries:
    if histories is None:
        histories = {
            FRONT: _front_history(),
            NEXT: _next_history(overtakes_from=SESSIONS[4]),
        }
    return build_continuous_series(
        underlying_id=UNDERLYING,
        terms=terms,
        histories=histories,
        sessions=sessions,
        policy=policy,
    )


def _contract_sequence(series: ContinuousFuturesSeries) -> tuple[InstrumentId, ...]:
    return tuple(bar.source_contract_id for bar in series.bars)


# --------------------------------------------------------------------------- #
# The roll rule
# --------------------------------------------------------------------------- #


def test_liquidity_crossover_rolls_on_the_session_after_it_is_observed() -> None:
    """Volume and open interest both favouring the next contract is the trigger."""
    series = _build()

    assert len(series.rolls) == 1
    roll = series.rolls[0]
    assert roll.reason is RollReason.LIQUIDITY_CROSSOVER
    assert roll.decision_date == SESSIONS[4]
    assert roll.roll_date == SESSIONS[5]
    assert _contract_sequence(series) == (FRONT,) * 5 + (NEXT,) * 7


def test_one_side_of_liquidity_crossing_alone_is_not_a_roll() -> None:
    """A single busy session in the back month is noise, not a migration."""
    volume_only = _series(
        NEXT,
        tuple(_candle(item, close="102", volume=5000, open_interest=400) for item in SESSIONS),
        token=222,
    )

    series = _build(histories={FRONT: _front_history(), NEXT: volume_only})

    assert [roll.reason for roll in series.rolls] == [RollReason.EXPIRY_FALLBACK]


def test_without_a_crossover_the_expiry_buffer_forces_the_roll() -> None:
    """Expiry is the floor: the series never rides a contract into settlement."""
    quiet = _series(
        NEXT,
        tuple(_candle(item, close="102", volume=10, open_interest=40) for item in SESSIONS),
        token=222,
    )

    series = _build(histories={FRONT: _front_history(), NEXT: quiet})

    roll = series.rolls[0]
    assert roll.reason is RollReason.EXPIRY_FALLBACK
    # Front expires on SESSIONS[9]; two sessions remain after SESSIONS[7].
    assert roll.decision_date == SESSIONS[7]
    assert roll.roll_date == SESSIONS[8]


def test_the_buffer_is_counted_in_sessions_not_calendar_days() -> None:
    """A wider buffer moves the roll by whole sessions, holidays included."""
    quiet = _series(
        NEXT,
        tuple(_candle(item, close="102", volume=10, open_interest=40) for item in SESSIONS),
        token=222,
    )
    calendar = SESSIONS[:5] + SESSIONS[8:]

    series = _build(
        histories={FRONT: _front_history(), NEXT: quiet},
        sessions=calendar,
        policy=RollPolicy(expiry_buffer_sessions=2),
    )

    # Only SESSIONS[8] and SESSIONS[9] remain at or before expiry after
    # SESSIONS[4], so the buffer fires there and not on a day count.
    assert series.rolls[0].decision_date == SESSIONS[4]
    assert series.rolls[0].roll_date == SESSIONS[8]


def test_a_front_contract_that_stops_printing_rolls_when_the_next_one_starts() -> None:
    """Expiry with a silent successor still produces one honest roll record."""
    front = FuturesContractTerms(
        contract_id=FRONT, instrument_token=111, expiry=SESSIONS[7], lot_size=750
    )
    late = _next_history(sessions=SESSIONS[10:])

    series = _build(
        terms=(front, NEXT_TERMS),
        histories={FRONT: _front_history(SESSIONS[:8]), NEXT: late},
    )

    roll = series.rolls[0]
    assert roll.reason is RollReason.EXPIRY_FALLBACK
    assert roll.decision_date == SESSIONS[7]
    assert roll.roll_date == SESSIONS[10]
    assert tuple(bar.trading_date for bar in series.bars) == SESSIONS[:8] + SESSIONS[10:]


def test_three_contracts_produce_two_recorded_rolls() -> None:
    """Each switch is recorded; none is inferred by the reader."""
    first = FuturesContractTerms(
        contract_id=FRONT, instrument_token=111, expiry=SESSIONS[3], lot_size=750
    )
    second = FuturesContractTerms(
        contract_id=NEXT, instrument_token=222, expiry=SESSIONS[7], lot_size=750
    )
    third = FuturesContractTerms(
        contract_id=FAR, instrument_token=333, expiry=SESSIONS[11], lot_size=750
    )
    quiet = tuple(_candle(item, close="102", volume=10, open_interest=40) for item in SESSIONS)

    series = _build(
        terms=(first, second, third),
        histories={
            FRONT: _front_history(SESSIONS[:4]),
            NEXT: _series(NEXT, quiet[:8], token=222),
            FAR: _series(FAR, quiet, token=333),
        },
        policy=RollPolicy(expiry_buffer_sessions=1),
    )

    assert [roll.reason for roll in series.rolls] == [
        RollReason.EXPIRY_FALLBACK,
        RollReason.EXPIRY_FALLBACK,
    ]
    assert [roll.to_contract_id for roll in series.rolls] == [NEXT, FAR]


def test_a_roll_reason_is_never_inherited_by_a_contract_it_was_not_about() -> None:
    """A decision naming one successor cannot explain a roll that landed elsewhere.

    The back month wins on liquidity, is rolled into, then stops printing and
    expires. The move on to the month after that is an expiry event, and saying
    "liquidity crossover" there would attribute it to evidence that was about a
    different contract. The gap in sessions is left visible rather than filled.
    """
    first = FuturesContractTerms(
        contract_id=FRONT, instrument_token=111, expiry=SESSIONS[4], lot_size=750
    )
    second = FuturesContractTerms(
        contract_id=NEXT, instrument_token=222, expiry=SESSIONS[6], lot_size=750
    )
    third = FuturesContractTerms(
        contract_id=FAR, instrument_token=333, expiry=SESSIONS[11], lot_size=750
    )

    series = _build(
        terms=(first, second, third),
        histories={
            FRONT: _front_history(SESSIONS[:5]),
            NEXT: _series(
                NEXT,
                tuple(
                    _candle(item, close="101", volume=9999, open_interest=99999)
                    for item in SESSIONS[:3]
                ),
                token=222,
            ),
            FAR: _series(
                FAR,
                tuple(
                    _candle(item, close="102", volume=50, open_interest=500) for item in SESSIONS
                ),
                token=333,
            ),
        },
        policy=RollPolicy(expiry_buffer_sessions=1),
    )

    assert [roll.reason for roll in series.rolls] == [
        RollReason.LIQUIDITY_CROSSOVER,
        RollReason.EXPIRY_FALLBACK,
    ]
    assert [roll.to_contract_id for roll in series.rolls] == [NEXT, FAR]
    assert tuple(bar.trading_date for bar in series.bars) == SESSIONS[:3] + SESSIONS[7:]


def test_a_single_contract_produces_a_series_with_no_rolls() -> None:
    """A series is not required to roll to be a series."""
    series = _build(terms=(FRONT_TERMS,), histories={FRONT: _front_history()})

    assert series.rolls == ()
    assert len(series.bars) == 10


def test_unsettled_sessions_never_decide_a_roll_or_enter_the_series() -> None:
    """An unsettled bar is a fact about today, not evidence about liquidity."""
    unsettled = _series(
        NEXT,
        tuple(_candle(item, close="102", volume=9999, open_interest=99999) for item in SESSIONS),
        token=222,
        incomplete_from=SESSIONS[0],
    )

    series = _build(histories={FRONT: _front_history(), NEXT: unsettled})

    assert _contract_sequence(series) == (FRONT,) * 10
    assert series.rolls == ()


# --------------------------------------------------------------------------- #
# Provenance and the absence of look-ahead
# --------------------------------------------------------------------------- #


def test_every_roll_is_decided_strictly_before_it_takes_effect() -> None:
    """The rule may not switch contracts on a session it has already read."""
    series = _build()

    assert series.rolls
    for roll in series.rolls:
        assert roll.decision_date < roll.roll_date


def test_every_bar_names_the_actual_contract_that_produced_it() -> None:
    """Source-contract provenance is on the bar, not reconstructed by the reader."""
    series = _build()

    for bar in series.bars:
        assert bar.source_contract_id in {FRONT, NEXT}
        assert bar.source_contract_id != series.underlying_id
        assert bar.source_instrument_token in {111, 222}
        assert bar.trading_date <= bar.source_expiry


def test_the_roll_records_the_evidence_and_the_visible_price_gap() -> None:
    """v1 leaves the stitch visible; a later adjustment policy consumes this gap."""
    roll = _build().rolls[0]

    assert roll.from_close == Decimal("100")
    assert roll.to_close == Decimal("102")
    assert roll.price_gap == Decimal("2")
    assert roll.from_volume == 1000
    assert roll.to_volume == 2000
    assert roll.from_open_interest == 5000
    assert roll.to_open_interest == 9000


def test_the_policy_revision_is_recorded_on_the_series_and_on_every_roll() -> None:
    """A result that cannot name the rule that made it is not reproducible."""
    series = _build()

    assert series.policy_revision == CONTINUOUS_FUTURES_POLICY_REVISION
    assert all(roll.policy_revision == series.policy_revision for roll in series.rolls)


def test_a_different_policy_produces_a_differently_versioned_series() -> None:
    """Two rules cannot share one revision string and stay distinguishable."""
    series = _build(policy=RollPolicy(expiry_buffer_sessions=4, revision="continuous-test-v2"))

    assert series.policy_revision == "continuous-test-v2"
    assert all(roll.policy_revision == "continuous-test-v2" for roll in series.rolls)


# --------------------------------------------------------------------------- #
# Research-only: the series is not, and cannot become, a tradable instrument
# --------------------------------------------------------------------------- #


def test_no_persistable_instrument_kind_represents_a_continuous_series() -> None:
    """There is no kind to store one as, so one can never reach the bar table.

    This is the load-bearing guarantee. `MarketInstrumentKind` is what the daily
    bar table and every provider request are keyed by; the day someone adds a
    CONTINUOUS member is the day a stitched price can be persisted and requested
    as though an exchange listed it.
    """
    assert set(MarketInstrumentKind) == {
        MarketInstrumentKind.CASH_EQUITY,
        MarketInstrumentKind.INDEX,
        MarketInstrumentKind.FUTURES_CONTRACT,
    }


def test_a_continuous_series_carries_no_instrument_identity_of_its_own() -> None:
    """It names its underlying and its sources, and claims no identity between."""
    series = _build()

    assert not hasattr(series, "instrument_id")
    assert not hasattr(series, "contract_id")
    assert not hasattr(series, "instrument_token")
    assert not hasattr(series.bars[0], "instrument_kind")
    assert not hasattr(series.bars[0], "instrument_id")


def test_execution_always_resolves_to_an_actual_contract() -> None:
    """The only bridge to a fill answers with something the exchange lists."""
    series = _build()

    for bar in series.bars:
        contract = series.execution_contract_on(bar.trading_date)
        assert contract.contract_id == bar.source_contract_id
        assert contract.contract_id != series.underlying_id
        assert contract.instrument_token == bar.source_instrument_token
        assert contract.trading_date == bar.trading_date


def test_execution_uses_the_then_effective_lot_size_across_a_roll() -> None:
    """Sizing follows the contract actually filled, not the one it replaced."""
    series = _build()
    roll = series.rolls[0]

    before = series.execution_contract_on(SESSIONS[4])
    after = series.execution_contract_on(roll.roll_date)

    assert before.contract_id == roll.from_contract_id
    assert before.lot_size == FRONT_TERMS.lot_size
    assert after.contract_id == roll.to_contract_id
    assert after.lot_size == NEXT_TERMS.lot_size
    assert before.lot_size != after.lot_size


def test_execution_on_a_session_the_series_does_not_have_is_refused() -> None:
    """An absent session yields no contract rather than the nearest one."""
    series = _build()

    with pytest.raises(MissingDataError, match="no session on the requested date"):
        series.execution_contract_on(date(2026, 6, 1))


def test_the_source_contract_may_change_only_on_a_recorded_roll_date() -> None:
    """A silent contract change would be an unexplained jump in the research price."""
    series = _build()
    changes = tuple(
        current.trading_date
        for previous, current in zip(series.bars, series.bars[1:], strict=False)
        if previous.source_contract_id != current.source_contract_id
    )

    assert changes == tuple(roll.roll_date for roll in series.rolls)


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_a_series_without_contracts_is_refused() -> None:
    """Nothing to stitch is a caller error, not an empty success."""
    with pytest.raises(ValidationError, match="at least one contract"):
        _build(terms=(), histories={})


def test_a_series_without_a_session_calendar_is_refused() -> None:
    """The buffer is counted in sessions, so there must be sessions to count."""
    with pytest.raises(ValidationError, match="session calendar"):
        _build(sessions=())


def test_an_unsorted_session_calendar_is_refused() -> None:
    """A calendar out of order would silently reorder the research series."""
    with pytest.raises(ValidationError, match="sorted and unique"):
        _build(sessions=(SESSIONS[2], SESSIONS[0], SESSIONS[1]))


def test_two_contracts_sharing_one_expiry_are_refused() -> None:
    """One underlying has one contract per expiry; two means a mapping defect."""
    twin = FuturesContractTerms(
        contract_id=NEXT, instrument_token=222, expiry=SESSIONS[9], lot_size=750
    )

    with pytest.raises(ValidationError, match="one contract per expiry"):
        _build(terms=(FRONT_TERMS, twin))


def test_a_contract_without_history_is_refused() -> None:
    """Stitching around an absent contract would hide the gap it leaves."""
    with pytest.raises(MissingDataError, match="missing a source contract's history"):
        _build(histories={FRONT: _front_history()})


def test_a_range_with_no_settled_session_is_refused() -> None:
    """An empty result must be an error, never an empty series."""
    with pytest.raises(MissingDataError, match="no contract had a settled session"):
        _build(
            terms=(FRONT_TERMS,),
            histories={
                FRONT: _series(
                    FRONT,
                    tuple(_candle(item) for item in SESSIONS[:3]),
                    incomplete_from=SESSIONS[0],
                )
            },
        )


def test_a_roll_cannot_move_backwards_in_expiry() -> None:
    """The domain refuses a roll record that reverses the contract order."""
    with pytest.raises(InvariantViolation, match="later expiry"):
        FuturesRoll(
            decision_date=SESSIONS[1],
            roll_date=SESSIONS[2],
            reason=RollReason.LIQUIDITY_CROSSOVER,
            from_contract_id=NEXT,
            to_contract_id=FRONT,
            from_expiry=SESSIONS[9],
            to_expiry=SESSIONS[3],
            from_close=Decimal("100"),
            to_close=Decimal("101"),
            from_volume=10,
            to_volume=20,
            from_open_interest=None,
            to_open_interest=None,
            policy_revision=CONTINUOUS_FUTURES_POLICY_REVISION,
        )


def test_a_bar_cannot_be_attributed_to_an_expired_contract() -> None:
    """Provenance that outlives its contract is not provenance."""
    with pytest.raises(InvariantViolation, match="expired contract"):
        ContinuousBar(
            trading_date=SESSIONS[5],
            candle=_candle(SESSIONS[5]),
            source_contract_id=FRONT,
            source_instrument_token=111,
            source_expiry=SESSIONS[4],
            source_lot_size=750,
        )
