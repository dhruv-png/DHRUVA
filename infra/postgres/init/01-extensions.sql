-- Extensions required before the first Alembic migration runs.
--
-- Executed once, on first container start, by the postgres entrypoint.
-- Schema itself is owned by Alembic (S04); nothing but extensions belongs here.

-- TimescaleDB: hypertables for ticks, bars and open interest (plan section 2.4).
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Deterministic UUID generation for surrogate keys (ADR-009).
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Trigram search for instrument lookup by partial symbol (S07).
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Query-performance visibility; the Definition of Done requires EXPLAIN review
-- of every hot-path query (plan section 10.1).
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Storage is UTC everywhere (ADR-006). Set at database level so a client that
-- forgets to set it still writes correct data.
ALTER DATABASE dhruva SET timezone TO 'UTC';
