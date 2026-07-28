"""Connection probe: prove the database is reachable and correctly provisioned."""
import asyncio
import os
import re
import sys

import asyncpg

EXTENSION_QUERY = "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"


async def main() -> int:
    url = re.sub(r"\+asyncpg", "", os.environ["DHRUVA_TEST_DATABASE_URL"])
    connection = await asyncpg.connect(url)
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
