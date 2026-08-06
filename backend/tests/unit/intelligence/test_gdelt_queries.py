"""A planned batch becomes exactly one documented DOC 2.0 expression.

The whole module is a string builder, and that is the point: the decision about
*what* to search lives in the domain, the decision about *how GDELT spells it*
lives here, and neither has to be re-tested when the other changes.
"""

from __future__ import annotations

from datetime import date
from typing import Final

import pytest

from dhruva.contexts.intelligence.domain.entity_linking import LinkableInstrument, MatchKind
from dhruva.contexts.intelligence.domain.search import (
    MAX_BATCH_SIZE,
    SearchBatch,
    SearchPhrase,
    plan_search_phrases,
)
from dhruva.contexts.intelligence.infrastructure.gdelt.feed import GdeltQuery
from dhruva.contexts.intelligence.infrastructure.gdelt.queries import (
    gdelt_expression,
    gdelt_queries_for,
    gdelt_query_for,
)
from dhruva.shared.errors import ValidationError
from dhruva.shared.identity import InstrumentId

pytestmark = pytest.mark.unit

TODAY: Final = date(2026, 8, 6)


def _phrase(text: str, symbol: str = "SBIN") -> SearchPhrase:
    return SearchPhrase(
        text=text,
        kind=MatchKind.COMPANY_NAME,
        instrument_id=InstrumentId.deterministic("reference", symbol),
        canonical_symbol=symbol,
    )


def _batch(*texts: str) -> SearchBatch:
    return SearchBatch(index=1, phrases=tuple(_phrase(text) for text in texts))


def test_one_phrase_is_quoted_on_its_own() -> None:
    """A single phrase needs no grouping, and grouping it would add noise."""
    assert gdelt_expression(("State Bank of India",)) == '"State Bank of India"'


def test_several_phrases_are_grouped_with_or() -> None:
    """The documented way to ask for any one of several phrases."""
    assert gdelt_expression(("Adani Power", "Adani Green")) == '("Adani Power" OR "Adani Green")'


def test_phrase_order_is_preserved() -> None:
    """The expression is as deterministic as the plan that produced it."""
    forward = gdelt_expression(("Canara Bank", "Bajaj Finance"))
    backward = gdelt_expression(("Bajaj Finance", "Canara Bank"))

    assert forward == '("Canara Bank" OR "Bajaj Finance")'
    assert forward != backward


def test_an_empty_batch_is_refused() -> None:
    """A request for nothing is a wasted request against a throttled service."""
    with pytest.raises(ValidationError, match="at least one phrase"):
        gdelt_expression(())


def test_a_blank_phrase_is_refused() -> None:
    """Whitespace would become an empty quoted term the provider cannot use."""
    with pytest.raises(ValidationError, match="must not be empty"):
        gdelt_expression(("Adani Power", "   "))


@pytest.mark.parametrize("bad", ['Adani "Power"', "Adani (Power)", "Adani Power)"])
def test_a_phrase_carrying_query_operators_is_refused_not_escaped(bad: str) -> None:
    """Nothing here escapes anything; the planner refuses these first.

    A second, independent refusal is deliberate. This module can be called with
    a hand-built batch, and a quote reaching the URL unescaped would change what
    was asked for without changing what was logged.
    """
    with pytest.raises(ValidationError, match="query operators"):
        gdelt_expression(("Adani Power", bad))


def test_an_oversized_batch_is_refused() -> None:
    """Past the reviewable maximum the request stops being readable in a log."""
    with pytest.raises(ValidationError, match="larger than"):
        gdelt_expression(tuple(f"Company Number {index}" for index in range(MAX_BATCH_SIZE + 1)))


def test_a_batch_becomes_a_query_carrying_the_configured_bounds() -> None:
    """Timespan and record cap come from configuration, not from the batch."""
    query = gdelt_query_for(_batch("Adani Power", "Adani Green"), timespan="12h", max_records=25)

    assert query == GdeltQuery(
        query='("Adani Power" OR "Adani Green")',
        timespan="12h",
        max_records=25,
    )


def test_the_query_asks_only_for_documented_parameters() -> None:
    """A parameter nobody documented is a parameter nobody can rely on."""
    query = gdelt_query_for(_batch("Adani Power"), timespan="1d", max_records=75)

    assert set(query.parameters()) == {
        "query",
        "mode",
        "format",
        "timespan",
        "maxrecords",
        "sort",
    }


def test_a_plan_becomes_one_query_per_batch_in_order() -> None:
    """Request count is batch count; nothing is merged or dropped in between."""
    universe = tuple(
        LinkableInstrument(
            instrument_id=InstrumentId.deterministic("reference", symbol),
            canonical_symbol=symbol,
            company_name=name,
        )
        for symbol, name in (
            ("ADANIPOWER", "Adani Power Limited"),
            ("BAJFINANCE", "Bajaj Finance Limited"),
            ("CANBK", "Canara Bank"),
        )
    )
    plan = plan_search_phrases(universe, on=TODAY, batch_size=2)
    queries = gdelt_queries_for(plan, timespan="1d", max_records=10)

    assert len(queries) == len(plan.batches) == 2
    assert queries[0].query == '("Adani Power" OR "Bajaj Finance")'
    assert queries[1].query == '"Canara Bank"'


def test_a_plan_with_no_batches_produces_no_queries() -> None:
    """Nothing to search for means nothing is asked."""
    plan = plan_search_phrases((), on=TODAY)

    assert gdelt_queries_for(plan, timespan="1d", max_records=10) == ()
