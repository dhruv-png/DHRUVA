# ADR-039 — Correlation propagates through contextvars, and threads require an explicit wrapper

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.4, section 4. Raised in the S02 design document and accepted on implementation.

## Context

An event-driven platform produces log archives that are only useful if a reader
can reconstruct causality. Three identifiers make that possible: what operation
this belongs to, what immediately caused it, and whose account it concerns.

Threading those through every function signature is unworkable. The alternative
is ambient propagation, which trades explicitness for reach.

## Decision

``contextvars`` carry ``correlation_id``, ``causation_id`` and ``account_id``.
They bind at process edges -- an HTTP request, a task, a tick batch -- and
propagate automatically across ``await`` boundaries.

Nested binds **narrow rather than reset**: an inner bind supplying only a
causation id inherits the outer correlation id. That inheritance is what makes
end-to-end tracing survive layering.

Binding is implemented as a class-based context manager rather than a
``@contextmanager`` generator. Measured: the generator machinery dominated the
cost, and this is on the hot path of every request and every tick batch. The
class form reduced it from 17.7 to roughly 2 microseconds per call.

``account_id`` is bound now and unused now, so no log line has to be retrofitted
when multi-tenancy activates at S44 (ADR-004).

Context does **not** reach threads or process pools. ``copy_context_into`` is the
sanctioned wrapper, and two tests document both the limitation and the remedy.

## Rationale

Ambient propagation is the right trade here specifically because the values are
observability metadata rather than business inputs. Nothing branches on a
correlation id; losing one degrades debuggability, not correctness. That is a
very different risk profile from ambient *configuration*, which ADR-031 forbids
precisely because behaviour does depend on it.

Unbound identifiers are omitted from records rather than emitted as null.
Emitting ``correlation_id: null`` on every record of a process that never binds
one is noise, and noise is what makes people stop reading logs.

## Consequences

The thread limitation is real and silent: work handed to a pool loses its
identifiers with no error. A test asserts the limitation exists so that it is
documented behaviour rather than a surprise, and the wrapper is tested alongside.

Every process edge must bind, or work downstream of it is untraceable. This
becomes a Definition of Done item for each subsystem that introduces an edge.
