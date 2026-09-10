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

## Implemented P0.2A feature boundary

`shadow.features` computes a deliberately small set of immutable close-price snapshots from a P0.1-validated chronological bar sequence: one-period simple returns, strict-window rolling mean, population variance and standard deviation, and z-scores. Each `FeatureSnapshot` identifies its instrument, feature, close input, implementation version, source dataset ID, trailing input count, market-observation time, propagated availability time, and semantic state. It is either `ready` with a finite `Decimal` value, `warming_up` with the explicit reason `insufficient_history`, or `unavailable` with an explicit reason such as `zero_variance`; failed calculations raise an error instead of becoming a snapshot.

The kernel validates through `validate_bars` and never sorts, deduplicates, fills, interpolates, or otherwise repairs observations. It groups only a supplied sequence's trailing observations for the same instrument. A snapshot's availability is the maximum availability of the inputs used to establish it, including partial history while warming up. This makes a value legal no earlier than every required input and makes appending later bars unable to rewrite prior snapshots.

P0.2A keeps feature arithmetic in the standard-library `Decimal` domain, with an isolated fixed 34-significant-digit, half-even calculation context and canonical finite-Decimal output. This preserves P0.1's boundary representation, avoids global Decimal-context dependence, and adds no numerical runtime dependency. Future vectorized internals, if warranted, must retain this domain boundary and demonstrate reproducible canonical output before being adopted.

## Authority and time

Strategies propose trades; risk authorizes or rejects them. Missing, stale, inconsistent, or invalid critical state results in no trade. Completed-bar decisions may only consume information modeled as available at that decision time; future OHLC path information cannot be used to justify a same-bar fill.

Event intelligence, if introduced, has observational authority: it may produce a typed, TTL-bounded `EventRiskState` for deterministic consumers. It cannot place orders, size positions, alter limits, override vetoes, mutate experiment parameters, or judge strategy profitability.

## Deliberate non-requirements

P0.0 does not need LangGraph, Redis, Kafka, Celery, Kubernetes, microservices, a web frontend, or Postgres. There is no concrete workload, deployment, queueing, shared-cache, multi-user, or persistence requirement that justifies their operational cost. Introduce a technology only behind a stable boundary when measured requirements demand it; do not turn future possibilities into present infrastructure.

## Implemented P0.3 strategy boundary

`shadow.strategies` contains one explicit, unvalidated hypothesis: a ready close z-score at or below a fixed entry threshold may propose `long_entry` while supplied non-authoritative lifecycle context is `flat`; a ready z-score at or above its fixed exit threshold may propose `exit` while that context is `holding`. The strategy consumes one immutable `FeatureSnapshot`, immutable typed configuration, supplied decision time, and supplied state, and returns an immutable `Signal` or `None`. Its signal records feature identity/version/window, value and observation/availability times, source dataset identity, configuration identity and material thresholds/freshness allowance, rule reason, decision/availability time, and strategy identity/version.

The strategy fails closed for absent, non-ready, mismatched, future-available, or stale feature evidence. It does not maintain positions, choose a holding horizon, submit orders, size positions, calculate fills, authorize risk, inspect future data, perform I/O, or claim profitability. A simulator in P0.4 must provide lifecycle and holding-elapsed context before any maximum-holding exit can be evaluated.
