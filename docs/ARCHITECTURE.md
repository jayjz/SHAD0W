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

## Implemented P0.4A timeline boundary

`shadow.simulation` is a small deterministic eligibility processor, not an event bus or a fill engine. It accepts immutable events for a market observation becoming available, a feature becoming available, a signal becoming available, and a modeled execution opportunity. An event's `effective_time` is the instant it may affect simulation: P0.1/P0.2 availability for upstream evidence, the P0.3 signal decision/availability time for signals, and an explicit modeled time for an execution opportunity. All are timezone-aware inputs normalized to UTC.

Events sort by `(effective_time, explicit causal precedence, instrument identifier, event id)`. The precedence is market observation, feature, signal, then execution opportunity; it is a documented mapping rather than enum or input order. A signal establishes a price-free eligibility boundary: an opportunity for the same instrument is chronologically legal only when its event time is **strictly greater than** signal availability. Thus equal-time precedence makes the trace inspectable but cannot make an equal-time completed-bar close executable. A legal opportunity is permission to attempt execution, not a fill; later opportunities remain legal while P0.5A leaves an action pending. `TimelineRecord` retains event, signal, opportunity, decision, and structured reason evidence. P0.4A itself creates no position, order, fill, price, cost, risk, or portfolio-accounting state.

## Implemented P0.4B lifecycle boundary

`shadow.simulation.lifecycle` is the sole owner of authoritative simulated lifecycle state. It composes P0.4A's canonical ordered events and strict eligibility trace; the timeline does not infer a position, and strategies retain only their non-authoritative input context. State is isolated by `Instrument` and is exactly one of `flat`, `pending_entry`, `holding`, or `pending_exit`. The initial hypothesis permits at most one open, quantity-free simulated position and one pending action per instrument.

A legal `long_entry` while flat creates an immutable entry action and moves to `pending_entry`; a legal `exit` while holding creates an exit action and moves to `pending_exit`. P0.5A creates an explicit attempt only at a P0.4A-legal opportunity. Only that attempt's `filled` outcome moves `pending_entry` to `holding` or `pending_exit` to `flat`; `unfilled` and `rejected` outcomes leave the action pending. Each `LifecycleRecord` captures the prior state, incoming event reference/time, decision/reason, resulting state, related immutable action/position evidence, and any execution attempt/outcome.

Same-time opposing entry/exit signals for an instrument reject both, independent of input order. The first canonical same-direction signal may create an action; later equivalent-direction signals are recorded as redundant. Entry while holding or while an exit is pending, exit while flat or while an entry is pending, and opportunities without an appropriate pending action are explicit non-actions. Pending actions do not expire in P0.4B: final `pending_entry`/`pending_exit`, plus final open positions, remain visible in `LifecycleResult`; no end-of-data liquidation occurs. `SimulatedPosition.opened_at` supplies an authoritative lifecycle clock for a future holding-horizon rule.

## Implemented P0.4C/P0.5A chronological runner and execution boundary

`shadow.simulation.run_simulation` composes P0.1--P0.5A for the sole current mean-reversion hypothesis. Its immutable `SimulationInput` contains a declared-validated P0.1 bar dataset, one P0.3 `MeanReversionConfig` per configured instrument, price-free P0.4A `ExecutionOpportunity` values, a tuple of existing provider-neutral P0.1 `Quote` evidence, a required immutable `QuoteExecutionConfig`, and an optional caller run label. The strategy configuration's required z-score identity and rolling window are the feature configuration; no redundant universal configuration object is introduced.

The runner calls P0.1 validation without sorting input, then reuses P0.2's z-score kernel. A decision point occurs exactly once per configured instrument and feature-availability instant. If delayed delivery makes several historical snapshots available together, the newest aligned observation is selected; the others remain structured feature evidence rather than creating backfill actions. The kernel's prefix-stable snapshots may be prepared from the immutable complete input, but the runner exposes the selected snapshot only at its legal availability instant; it never selects a later snapshot as evidence for an earlier instant. At a feature instant, P0.4A precedence means same-time feature events occur before any signal or execution opportunity. The runner therefore obtains P0.4B's state from strictly earlier signals/opportunities, supplies `flat` or `holding` as the P0.3 context, and fail-closes while P0.4B reports `pending_entry` or `pending_exit`. P0.3 remains stateless and P0.4B remains the only lifecycle owner.

`shadow.execution` owns the P0.5A quote baseline extended by P0.5B, `shadow.execution.quote_bid_ask.v2`. It selects the newest matching quote with `availability_time <= attempt_time`, then breaks equal availability/observation times by the stable complete-quote reference, never caller order. Freshness separately measures `attempt_time - quote.observation_time` against the explicit configuration. A long entry buys at ask and a long exit sells at bid; a locked quote is usable under P0.1 semantics, while a crossed quote is retained as evidence and produces `rejected`, never normalization. No matching, not-yet-available, or stale quote produces `unfilled`. A rejected attempt also remains pending under the P0.5A policy, permitting a later legal opportunity to be modeled independently.

The immutable `SimulationResult` retains metadata plus P0.1 fingerprint, canonical strategy configurations, execution configuration and quote evidence, P0.2 implementation version and snapshots, one structured disposition for every intended decision point, generated P0.3 signals, P0.4A timeline evidence, P0.4B lifecycle evidence/final state, and P0.5A attempts/outcomes. It contains modeled execution prices only in successful outcomes; it intentionally contains no quantity, commission, extra spread deduction, liquidity model, cash, P&L, return, or metric. The ask/bid sides already expose quoted spread, so no arbitrary additional spread charge is applied. For this baseline, admitting a signal to the research lifecycle is a simulation assumption, not future independent risk authorization. The initial hypothesis retains its threshold exit and does not define a maximum holding horizon, so the runner does not manufacture one from `opened_at`; end-of-stream pending and open states stay visible.

P0.5B extends the existing execution configuration/outcome boundary with one deterministic adverse bps transform after quote-side fill validation. Outcomes retain their configuration and derive the baseline side price from the exact selected quote; modeled price is separate. The runner needs no additional plumbing and lifecycle authority is unchanged. Caller-supplied sensitivity returns immutable outcomes in canonical ascending bps order, without economic evaluation. Model v2 records the materially changed price semantics; [the data contract](DATA_CONTRACTS.md#p05b-deterministic-adverse-slippage) defines Decimal rounding and numeric-domain limits.
