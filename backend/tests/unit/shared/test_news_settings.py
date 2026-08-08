"""News configuration is bounded, credential-free, and cannot contradict itself.

Every bound here is a bound on what DHRUVA will ask of a free shared service
that has already throttled it once. A configuration that loads is a promise that
the request it describes is one somebody chose.
"""

from __future__ import annotations

import os

import pytest

from dhruva.contexts.intelligence.domain.search import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
)
from dhruva.contexts.intelligence.infrastructure.gdelt.feed import GDELT_DOC_ENDPOINT
from dhruva.contexts.intelligence.infrastructure.gdelt.mapper import MAX_ARTICLES
from dhruva.shared.config.settings import NewsSettings, load_settings
from dhruva.shared.errors import ConfigurationError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove inherited DHRUVA_* variables so tests do not affect each other."""
    for key in list(os.environ):
        if key.startswith("DHRUVA_"):
            monkeypatch.delenv(key, raising=False)


def test_the_defaults_reach_the_documented_anonymous_endpoint() -> None:
    """No key, no token, no session -- and the URL the adapter actually calls."""
    settings = NewsSettings()

    assert settings.gdelt_endpoint == GDELT_DOC_ENDPOINT
    assert "key" not in settings.model_dump()
    assert "token" not in settings.model_dump()


def test_the_configured_endpoint_agrees_with_the_adapter_constant() -> None:
    """Two copies of one URL, kept honest by a test rather than by hope.

    The configuration module may not import a context (boundary rule R5/R4), so
    the default is written out. This is the check that keeps the copy correct.
    """
    assert NewsSettings().gdelt_endpoint == GDELT_DOC_ENDPOINT


def test_the_default_batch_size_agrees_with_the_planner() -> None:
    """One conservative default, defended in one place."""
    assert NewsSettings().batch_size == DEFAULT_BATCH_SIZE


def test_the_record_cap_stays_inside_what_the_mapper_will_accept() -> None:
    """Asking for more than the mapper reads would discard the difference."""
    assert NewsSettings().max_records <= MAX_ARTICLES
    assert NewsSettings(max_records=MAX_ARTICLES).max_records == MAX_ARTICLES


def test_watchlist_querying_is_on_by_default() -> None:
    """The approved universe is the point; a manual query is the exception."""
    assert NewsSettings().watchlist_queries_enabled is True


def test_an_override_turns_watchlist_querying_off_structurally() -> None:
    """One decision, one switch.

    Two independent switches encoding one decision is how a configuration ends
    up contradicting itself, with the code quietly picking a winner.
    """
    settings = NewsSettings(query_override='"State Bank of India"')

    assert settings.watchlist_queries_enabled is False
    assert settings.query_override == '"State Bank of India"'


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_override_is_refused(blank: str) -> None:
    """An unset environment variable looks exactly like this.

    Accepting it would switch the pass out of watchlist mode and then ask the
    provider for nothing at all.
    """
    with pytest.raises(ValueError, match="empty"):
        NewsSettings(query_override=blank)


@pytest.mark.parametrize("bad", ['"unbalanced', '("a" OR "b"', 'a) OR "b"'])
def test_an_unbalanced_override_is_refused(bad: str) -> None:
    """A half-written expression asks for something nobody intended."""
    with pytest.raises(ValueError, match="unbalanced"):
        NewsSettings(query_override=bad)


def test_an_enormous_override_is_refused() -> None:
    """A query that will not fit in a log line is a program, not a search."""
    with pytest.raises(ValueError, match="longer than"):
        NewsSettings(query_override='"x"' * 500)


@pytest.mark.parametrize("size", [0, -1, MAX_BATCH_SIZE + 1])
def test_an_impossible_batch_size_is_refused(size: int) -> None:
    """An unbounded batch is an unbounded demand on a free service."""
    with pytest.raises(ValueError, match=r"batch_size|less than or equal|greater than or equal"):
        NewsSettings(batch_size=size)


@pytest.mark.parametrize("size", [MIN_BATCH_SIZE, DEFAULT_BATCH_SIZE, MAX_BATCH_SIZE])
def test_the_advertised_batch_sizes_load(size: int) -> None:
    """The documented bounds are the bounds that work."""
    assert NewsSettings(batch_size=size).batch_size == size


@pytest.mark.parametrize("records", [0, -5, MAX_ARTICLES + 1])
def test_an_impossible_record_count_is_refused(records: int) -> None:
    """Zero cannot answer anything; more than the ceiling is discarded."""
    with pytest.raises(ValueError, match=r"less than or equal|greater than or equal"):
        NewsSettings(max_records=records)


@pytest.mark.parametrize("timespan", ["15min", "12h", "1d", "3w", "1m", "180d"])
def test_the_documented_timespans_are_accepted(timespan: str) -> None:
    """Exactly the forms GDELT documents."""
    assert NewsSettings(timespan=timespan).timespan == timespan


@pytest.mark.parametrize("timespan", ["", "d", "0d", "-1d", "1", "1y", "1 d", "1dd", "99999d"])
def test_an_undocumented_timespan_is_refused(timespan: str) -> None:
    """Guessing at a provider's accepted values is how a pass silently returns nothing."""
    with pytest.raises(ValueError, match="timespan"):
        NewsSettings(timespan=timespan)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://api.gdeltproject.org/api/v2/doc/doc",
        "https://user:pass@api.gdeltproject.org/api/v2/doc/doc",
        "https://api.gdeltproject.org/api/v2/doc/doc?query=already",
        "not-a-url",
    ],
)
def test_an_unusable_endpoint_is_refused(endpoint: str) -> None:
    """Plain https, no credentials, and no query somebody smuggled in."""
    with pytest.raises(ValueError, match="endpoint"):
        NewsSettings(gdelt_endpoint=endpoint)


def test_there_is_no_proxy_setting() -> None:
    """The response to being throttled is fewer requests, not another address."""
    fields = set(NewsSettings.model_fields)

    assert not any("proxy" in field for field in fields)
    assert not any("key" in field or "secret" in field or "token" in field for field in fields)


def test_an_unknown_news_key_is_refused_rather_than_ignored() -> None:
    """An unrecognised key is a typo in a deployment, not an opinion."""
    with pytest.raises(ValueError, match="Extra inputs"):
        NewsSettings(gdlet_endpoint="https://example.test")  # type: ignore[call-arg]


def test_news_settings_load_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The group is wired into the process configuration, not just defined."""
    monkeypatch.setenv("DHRUVA_NEWS__BATCH_SIZE", "3")
    monkeypatch.setenv("DHRUVA_NEWS__TIMESPAN", "12h")

    settings = load_settings()

    assert settings.news.batch_size == 3
    assert settings.news.timespan == "12h"


def test_invalid_news_configuration_stops_the_process_naming_the_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail fast, and say which variable is wrong (ADR-031)."""
    monkeypatch.setenv("DHRUVA_NEWS__BATCH_SIZE", "9999")

    with pytest.raises(ConfigurationError) as error:
        load_settings()

    assert "news.batch_size" in error.value.context["fields"]


def test_the_result_limit_and_lookback_are_bounded() -> None:
    """A read command is an operator tool, not an export."""
    with pytest.raises(ValueError, match=r"less than or equal|greater than or equal"):
        NewsSettings(result_limit=0)
    with pytest.raises(ValueError, match=r"less than or equal|greater than or equal"):
        NewsSettings(lookback_days=0)
    assert NewsSettings().result_limit >= 1
    assert NewsSettings().lookback_days >= 1


def test_the_timeout_is_bounded_at_both_ends() -> None:
    """No timeout is a hung command; a huge one is the same thing slower."""
    with pytest.raises(ValueError, match="greater than or equal"):
        NewsSettings(timeout_seconds=0.0)
    with pytest.raises(ValueError, match="less than or equal"):
        NewsSettings(timeout_seconds=600.0)


def test_the_minimum_request_interval_defaults_to_one_conservative_second() -> None:
    """A courteous default for an anonymous free service with no published limit."""
    assert NewsSettings().min_request_interval_seconds == 1.0


def test_a_zero_request_interval_is_the_documented_no_pacing_escape_hatch() -> None:
    """A caller that wants no pacing at all -- such as a test -- may ask for it."""
    assert NewsSettings(min_request_interval_seconds=0.0).min_request_interval_seconds == 0.0


def test_the_minimum_request_interval_is_bounded_at_both_ends() -> None:
    """Negative is meaningless and an unbounded ceiling could hang a four-batch pass."""
    with pytest.raises(ValueError, match="greater than or equal"):
        NewsSettings(min_request_interval_seconds=-0.1)
    with pytest.raises(ValueError, match="less than or equal"):
        NewsSettings(min_request_interval_seconds=10.1)
    assert NewsSettings(min_request_interval_seconds=10.0).min_request_interval_seconds == 10.0


def test_the_request_interval_loads_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The group is wired into process configuration, not just defined."""
    monkeypatch.setenv("DHRUVA_NEWS__MIN_REQUEST_INTERVAL_SECONDS", "2.5")

    settings = load_settings()

    assert settings.news.min_request_interval_seconds == 2.5
