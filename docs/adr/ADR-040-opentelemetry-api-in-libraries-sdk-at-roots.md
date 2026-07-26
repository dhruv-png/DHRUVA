# ADR-040 — OpenTelemetry API in library code; the SDK only at composition roots

- **Status:** Accepted
- **Date:** 2026-07-26
- **Deciders:** Product Owner (drew), CTO / Principal Architect
- **Supersedes:** —
- **Superseded by:** —

> Origin: Master Project Plan v1.4, section 4. Raised in the S02 design document and accepted on implementation.

## Context

Tracing has to be reachable from anywhere, but configuring an exporter is a
deployment concern. The usual mistake is to make library code depend on the SDK,
which drags an exporter, a batch processor and a resource detector into every
test run and every consumer.

## Decision

Library code imports the **OpenTelemetry API** only. The **SDK** is an optional
dependency extra, installed and configured at composition roots and in deployed
environments.

The API is a no-op facade when no SDK is installed, so tests and library
consumers pay nothing. No wrapper ``Tracer`` port of our own is introduced.

Because a no-op tracer is indistinguishable from a broken one -- spans simply
vanish -- ``tracing_is_active()`` reports whether a real provider is installed,
and the startup banner states it. A configuration that enables tracing without a
provider logs a warning naming the consequence.

## Rationale

Wrapping a facade in a second facade was considered and rejected. The API layer
already provides exactly the indirection a port would, is stable, and is
understood by everyone who has used OpenTelemetry. Adding our own would create a
type to learn with no capability gained -- the opposite of the ports-and-adapters
reasoning in ADR-003, where the wrapped thing (a broker) is genuinely
substitutable and genuinely leaky.

Reporting tracing status at startup exists because the failure mode is absence.
Every other misconfiguration in this system raises; this one produces silence,
and silence is indistinguishable from "no traffic".

## Consequences

Deployed environments must install the ``tracing`` extra, and a test asserts the
SDK is not a runtime dependency so the split cannot erode.

Trace context propagation across process boundaries -- into Celery tasks and
Redis Streams consumers -- is not solved here. It arrives with S05, where the
event envelope gains carrier fields.
