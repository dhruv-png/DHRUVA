#!/usr/bin/env python
"""Credential-free live smoke test for the GDELT DOC 2.0 adapter.

Polls GDELT once with the real transport, prints what came back, and writes
nothing to the database. It exists because DHRUVA's build environment has no
route to `api.gdeltproject.org`, so every automated test runs against
hand-written fixtures and the first contact with the live endpoint is
necessarily a human running this.

No API key, no credential, no configuration file. GDELT is anonymous.

What this proves, and what it does not
--------------------------------------
Reaching the endpoint at all -- even with an HTTP error -- proves DNS, TLS,
routing and the endpoint path. Only a `HEALTHY` or `EMPTY_RESULT` outcome also
proves the response *schema* is the one the mapper reads.

`RATE_LIMITED` is therefore a partial success: the endpoint is real and the
adapter classified the refusal correctly. It is not a defect in DHRUVA, and it
is deliberately never reported as an empty successful poll. GDELT publishes no
rate-limit policy, so the right response is to wait and try again later, with a
smaller query.

The default query is the smallest one that can prove schema compatibility: a
single phrase, one record. Politeness toward a free shared service is the point,
not thoroughness -- ingestion uses its own, larger query.

Usage
-----
    uv run --project backend python scripts/gdelt_smoke_test.py
    uv run --project backend python scripts/gdelt_smoke_test.py --retry-once
    uv run --project backend python scripts/gdelt_smoke_test.py --query '"Adani Ports"'
    uv run --project backend python scripts/gdelt_smoke_test.py --save-fixture out.json

Exit status is 0 when the poll reached a usable answer -- including a genuinely
empty one -- and 1 for any operational failure, so it can gate a runbook step.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx2
from dhruva.contexts.intelligence.domain.sources import NewsFetchResult, SourceHealth
from dhruva.contexts.intelligence.infrastructure.gdelt.feed import (
    GDELT_DOC_ENDPOINT,
    GdeltDocFeed,
    GdeltQuery,
)
from dhruva.contexts.intelligence.infrastructure.gdelt.mapper import (
    GDELT_ATTRIBUTION_URL,
    map_artlist,
)

#: One phrase, one record. The smallest request that can still return a
#: documented article and prove the schema.
DEFAULT_QUERY = '"State Bank of India"'
DEFAULT_TIMESPAN = "1d"
DEFAULT_MAX_RECORDS = 1

_TIMEOUT_SECONDS = 30.0
_PREVIEW = 10
#: Used only with --retry-once, and only when the server supplied no Retry-After.
DEFAULT_RETRY_DELAY_SECONDS = 60.0
#: However long the server asks for, this script will not sit there all day.
MAX_RETRY_DELAY_SECONDS = 300.0

#: Outcomes worth one bounded second attempt, when explicitly requested.
_RETRYABLE = frozenset(
    {SourceHealth.RATE_LIMITED, SourceHealth.TEMPORARILY_UNAVAILABLE}
)


class SystemClock:
    """Return the real current instant, in UTC."""

    def now(self) -> datetime:
        """Return the current instant as timezone-aware UTC."""
        return datetime.now(UTC)


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live GDELT DOC 2.0 smoke test (anonymous, read-only, no database write).",
    )
    parser.add_argument(
        "--query", default=DEFAULT_QUERY, help="DOC 2.0 query expression"
    )
    parser.add_argument(
        "--timespan", default=DEFAULT_TIMESPAN, help="documented timespan, e.g. 1d, 12h"
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=DEFAULT_MAX_RECORDS,
        help="1..250 (default 1)",
    )
    parser.add_argument(
        "--retry-once",
        action="store_true",
        help=(
            "after a rate-limited or unavailable poll, wait once and try exactly "
            "one more time. Off by default."
        ),
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=DEFAULT_RETRY_DELAY_SECONDS,
        help=(
            f"seconds to wait before the single retry when the server supplies no "
            f"Retry-After (default {DEFAULT_RETRY_DELAY_SECONDS:.0f}, "
            f"capped at {MAX_RETRY_DELAY_SECONDS:.0f})"
        ),
    )
    parser.add_argument(
        "--save-fixture",
        type=Path,
        default=None,
        help="write the raw response here so it can be sanitised into a test fixture",
    )
    return parser.parse_args()


def _report(result: NewsFetchResult) -> None:
    """Print everything the poll observed, including what was absent."""
    print(f"health     : {result.status.health.value}")
    print(f"reason     : {result.status.reason}")
    print(
        "http       : "
        + (
            str(result.status.http_status)
            if result.status.http_status is not None
            else "n/a"
        )
    )
    # Printed unconditionally: "the server did not say" is itself a finding, and
    # a line that only appears sometimes leaves the operator guessing which.
    print(
        "retry-after: "
        + (
            f"{result.status.retry_after.total_seconds():.0f}s"
            if result.status.retry_after is not None
            else "not supplied by the server"
        )
    )
    print(f"sha256     : {result.content_sha256}")
    print(f"mapper     : {result.mapper_revision}")
    print(f"items      : {len(result.items)}\n")

    for item in result.items[:_PREVIEW]:
        print(f"  {item.published_at.isoformat()}  {item.source.display_name}")
        print(f"    {item.text.title}")
        print(f"    {item.identity.url}")
    if len(result.items) > _PREVIEW:
        print(f"  ... and {len(result.items) - _PREVIEW} more")


def _retry_delay(result: NewsFetchResult, requested: float) -> float:
    """Return the bounded wait before the one permitted retry."""
    supplied = (
        result.status.retry_after.total_seconds()
        if result.status.retry_after is not None
        else requested
    )
    return max(0.0, min(supplied, MAX_RETRY_DELAY_SECONDS))


def _verdict(result: NewsFetchResult) -> None:
    """Explain what this outcome does and does not establish."""
    if result.status.health is SourceHealth.HEALTHY:
        print("\nEndpoint reachable and response schema understood. Adapter verified.")
    elif result.status.health is SourceHealth.EMPTY_RESULT:
        print(
            "\nEndpoint reachable and response schema understood; the query simply "
            "matched nothing. Adapter verified."
        )
    elif result.status.health is SourceHealth.RATE_LIMITED:
        print(
            "\nEndpoint reachable and the refusal was classified correctly, so this "
            "is NOT a DHRUVA defect. Schema compatibility remains unproven.\n"
            "GDELT publishes no rate-limit policy. Wait a while and rerun, or rerun "
            "with --retry-once."
        )
    else:
        print(
            f"\nThe poll did not reach a usable answer ({result.status.health.value}). "
            "Schema compatibility remains unproven."
        )


async def _poll(client: httpx2.AsyncClient, query: GdeltQuery) -> NewsFetchResult:
    """Run one poll through the production adapter."""
    return await GdeltDocFeed(client, SystemClock(), query).fetch()


async def _run(arguments: argparse.Namespace) -> int:
    query = GdeltQuery(
        query=arguments.query,
        timespan=arguments.timespan,
        max_records=arguments.max_records,
    )
    print(f"endpoint   : {GDELT_DOC_ENDPOINT}")
    print(f"query      : {query.query}")
    print(f"timespan   : {query.timespan}   maxrecords: {query.max_records}")
    print("credential : none (GDELT is anonymous)")
    print(f"retry-once : {'yes' if arguments.retry_once else 'no (default)'}\n")

    async with httpx2.AsyncClient(
        timeout=_TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        if arguments.save_fixture is not None:
            response = await client.get(
                GDELT_DOC_ENDPOINT,
                params=query.parameters(),
                headers={"Accept": "application/json"},
            )
            arguments.save_fixture.write_bytes(response.content)
            print(f"raw response written to {arguments.save_fixture}\n")
            result = map_artlist(response.content, retrieved_at=SystemClock().now())
        else:
            result = await _poll(client, query)

        _report(result)

        # Exactly one extra attempt, only when asked for, and only after waiting
        # at least as long as the server requested. Anything more would be
        # working around a rate limit rather than respecting it.
        if arguments.retry_once and result.status.health in _RETRYABLE:
            delay = _retry_delay(result, arguments.retry_delay)
            print(f"\n--retry-once: waiting {delay:.0f}s, then one further attempt.\n")
            await asyncio.sleep(delay)
            result = await _poll(client, query)
            _report(result)

    _verdict(result)
    print(f"\nData source: The GDELT Project — {GDELT_ATTRIBUTION_URL}")
    print("Nothing was written to the database. No article body was fetched.")
    return 0 if result.status.succeeded else 1


def main() -> int:
    """Run one live poll and report its outcome."""
    arguments = _parse_arguments()
    if arguments.retry_delay < 0:
        print("--retry-delay cannot be negative", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run(arguments))
    except KeyboardInterrupt:  # pragma: no cover - operator interrupt
        return 130


if __name__ == "__main__":
    sys.exit(main())
