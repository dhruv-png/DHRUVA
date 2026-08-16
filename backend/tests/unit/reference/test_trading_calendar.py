"""The interim configured NSE cash-calendar adapter."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from dhruva.contexts.reference.infrastructure import ConfiguredNseCashCalendar
from dhruva.shared.time import DateRange, TradingCalendar, TradingDay

pytestmark = pytest.mark.unit


def test_adapter_satisfies_the_shared_calendar_port() -> None:
    """Routine refresh reuses ADR-046's port rather than a second calendar shape."""
    assert isinstance(ConfiguredNseCashCalendar(), TradingCalendar)


def test_regular_cash_session_bounds_are_utc_instants() -> None:
    """The regular 09:15-15:30 IST session is represented under ADR-006."""
    calendar = ConfiguredNseCashCalendar()
    session = calendar.session(TradingDay.of(date(2026, 8, 14), calendar))

    assert session.opens_at == datetime(2026, 8, 14, 3, 45, tzinfo=UTC)
    assert session.closes_at == datetime(2026, 8, 14, 10, 0, tzinfo=UTC)


def test_sessions_between_skips_weekends_and_configured_closed_dates() -> None:
    """Known weekday closures and weekends share one calendar-owned path."""
    calendar = ConfiguredNseCashCalendar(closed_dates=frozenset({date(2026, 8, 17)}))

    sessions = calendar.sessions_between(DateRange(date(2026, 8, 14), date(2026, 8, 19)))

    assert [session.on for session in sessions] == [date(2026, 8, 14), date(2026, 8, 18)]
