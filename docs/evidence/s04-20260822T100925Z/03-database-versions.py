"""Connection probe: prove the database is reachable and correctly provisioned."""
import asyncio
import os
import re
import sys

import asyncpg

EXTENSION_QUERY = "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"

#: How long to keep retrying a *transient* connection failure.
CONNECT_DEADLINE_SECONDS = 90.0

#: Failures that mean "not up yet" rather than "wrong". A server that is still
#: starting accepts the socket and closes it again, which asyncpg surfaces as
#: ConnectionError("unexpected connection_lost() call") from inside SSL
#: negotiation -- an unhelpful message for an entirely ordinary race.
TRANSIENT = (
    ConnectionError,
    OSError,
    asyncpg.CannotConnectNowError,
    asyncpg.TooManyConnectionsError,
)


async def connect_with_retry(url: str) -> asyncpg.Connection:
    """Connect, retrying only failures that a wait could plausibly cure.

    A wrong password or a missing database is not retried: sitting in a loop for
    ninety seconds re-sending bad credentials turns an instant, clearly worded
    failure into a slow, vague one.
    """
    deadline = asyncio.get_event_loop().time() + CONNECT_DEADLINE_SECONDS
    delay = 0.5
    attempt = 0
    while True:
        attempt += 1
        try:
            return await asyncpg.connect(url)
        except TRANSIENT as error:
            if asyncio.get_event_loop().time() >= deadline:
                print(
                    f"FATAL: no usable connection after {attempt} attempts "
                    f"over {CONNECT_DEADLINE_SECONDS:.0f}s. Last error: {error!r}"
                )
                raise
            print(f"  attempt {attempt}: {type(error).__name__} -- retrying in {delay:.1f}s")
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 5.0)


async def main() -> int:
    url = re.sub(r"\+asyncpg", "", os.environ["DHRUVA_TEST_DATABASE_URL"])
    connection = await connect_with_retry(url)
    try:
        print("postgresql: ", await connection.fetchval("SHOW server_version"))
        extension = await connection.fetchval(EXTENSION_QUERY)
        print("timescaledb:", extension or "NOT INSTALLED")
        print("isolation:  ", await connection.fetchval("SHOW transaction_isolation"))
        print("database:   ", await connection.fetchval("SELECT current_database()"))
        print("user:       ", await connection.fetchval("SELECT current_user"))
    finally:
        await connection.close()

    if not extension:
        # Three integration tests exercise hypertable DDL, which does not exist
        # without the extension. Continuing would produce failures that look like
        # defects in the persistence layer.
        print("FATAL: the timescaledb extension is not installed in this database.")
        return 1
    return 0


sys.exit(asyncio.run(main()))
