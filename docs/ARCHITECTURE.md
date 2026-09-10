# Architecture

## Shape

SHAD0W begins as a Python modular monolith with explicit ports/adapters boundaries. It must be easy to run deterministically from recorded inputs before it is easy to connect to external systems. A module should own one of these concepts:

| Area | Responsibility |
| --- | --- |
| domain | Provider-neutral value types and business rules |
| data | Ingestion adapters, normalization, validation, provenance |
| features | Deterministic calculations with availability timestamps |
| strategies | Explicit hypotheses that propose signals only |
| regimes | Deterministic market-state classification |
| risk | Independent authorization or veto of proposed trades |
| execution | Ports and later adapters for cost, fill, and broker behavior |
| evaluation | Chronological simulation outputs and descriptive metrics |
| evidence | Structured experiment and decision reconstruction |
| event intelligence | Future asynchronous unstructured-event classification into typed state |
| CLI/application | Composition, commands, and configuration loading |

The intended flow is data → normalization/validation → features → strategy signal → regime → risk → decision → execution/cost model → simulation or later execution surface → evidence/evaluation. Every boundary carries typed domain values rather than provider SDK, Pandas, HTTP, or LLM response objects.

## Implemented P0.1 data boundary

The current `domain` and `data` modules provide immutable provider-neutral `Bar`, `Quote`, provenance, and dataset-metadata values plus fail-closed collection validation and canonical dataset identity. They deliberately contain no ingestion adapter: a future adapter must translate provider timestamps and source conventions before creating these values. The boundary accepts neither provider responses nor dataframe objects.

For a `Bar`, `observation_time` is the **end** of its represented interval. `availability_time` is the earliest modeled instant at which a strategy may consume the completed record; the source of that assertion is an explicit availability-semantics value. Both timestamps are normalized to UTC, while source timezone/session descriptions remain provenance. This permits a later simulation to enforce availability without retroactively changing a record.

## Authority and time

Strategies propose trades; risk authorizes or rejects them. Missing, stale, inconsistent, or invalid critical state results in no trade. Completed-bar decisions may only consume information modeled as available at that decision time; future OHLC path information cannot be used to justify a same-bar fill.

Event intelligence, if introduced, has observational authority: it may produce a typed, TTL-bounded `EventRiskState` for deterministic consumers. It cannot place orders, size positions, alter limits, override vetoes, mutate experiment parameters, or judge strategy profitability.

## Deliberate non-requirements

P0.0 does not need LangGraph, Redis, Kafka, Celery, Kubernetes, microservices, a web frontend, or Postgres. There is no concrete workload, deployment, queueing, shared-cache, multi-user, or persistence requirement that justifies their operational cost. Introduce a technology only behind a stable boundary when measured requirements demand it; do not turn future possibilities into present infrastructure.
