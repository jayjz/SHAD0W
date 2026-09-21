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

## Implemented P0.4C/P0.5 chronological runner and execution boundaries

`shadow.simulation.run_simulation` composes P0.1--P0.5C for the sole current mean-reversion hypothesis. Its immutable `SimulationInput` contains a declared-validated P0.1 bar dataset, one P0.3 `MeanReversionConfig` per configured instrument, price-free P0.4A `ExecutionOpportunity` values, a tuple of existing provider-neutral P0.1 `Quote` evidence, a required immutable `QuoteExecutionConfig`, optional P0.5C economics configurations, and an optional caller run label. `None` explicitly disables economics. When enabled, exactly one economics configuration must cover each configured strategy instrument; duplicates, missing instruments, and extras fail before simulation, and configurations are ordered canonically. The strategy configuration's required z-score identity and rolling window remain the feature configuration; no redundant universal feature configuration is introduced.

The runner calls P0.1 validation without sorting input, then reuses P0.2's z-score kernel. A decision point occurs exactly once per configured instrument and feature-availability instant. If delayed delivery makes several historical snapshots available together, the newest aligned observation is selected; the others remain structured feature evidence rather than creating backfill actions. The kernel's prefix-stable snapshots may be prepared from the immutable complete input, but the runner exposes the selected snapshot only at its legal availability instant; it never selects a later snapshot as evidence for an earlier instant. At a feature instant, P0.4A precedence means same-time feature events occur before any signal or execution opportunity. The runner therefore obtains P0.4B's state from strictly earlier signals/opportunities, supplies `flat` or `holding` as the P0.3 context, and fail-closes while P0.4B reports `pending_entry` or `pending_exit`. P0.3 remains stateless and P0.4B remains the only lifecycle owner.

`shadow.execution` owns the P0.5A quote baseline extended by P0.5B, `shadow.execution.quote_bid_ask.v2`. It selects the newest matching quote with `availability_time <= attempt_time`, then breaks equal availability/observation times by the stable complete-quote reference, never caller order. Freshness separately measures `attempt_time - quote.observation_time` against the explicit configuration. A long entry buys at ask and a long exit sells at bid; a locked quote is usable under P0.1 semantics, while a crossed quote is retained as evidence and produces `rejected`, never normalization. No matching, not-yet-available, or stale quote produces `unfilled`. A rejected attempt also remains pending under the P0.5A policy, permitting a later legal opportunity to be modeled independently.

The immutable `SimulationResult` retains metadata plus P0.1 fingerprint, canonical strategy configurations, execution and optional economics configurations, quote evidence, P0.2 implementation version and snapshots, one structured disposition for every intended decision point, generated P0.3 signals, P0.4A timeline evidence, P0.4B lifecycle evidence/final state, P0.5A/P0.5B attempts/outcomes, and P0.5C economic executions. It contains no cash balance, portfolio accounting, P&L, return, or performance metric. The ask/bid sides already expose quoted spread, so no arbitrary additional spread charge is applied. For this baseline, admitting a signal to the research lifecycle is a simulation assumption, not future independent risk authorization. The initial hypothesis retains its threshold exit and does not define a maximum holding horizon, so the runner does not manufacture one from `opened_at`; end-of-stream pending and open states stay visible.

P0.5B extends the existing execution configuration/outcome boundary with one deterministic adverse bps transform after quote-side fill validation. Outcomes retain their configuration and derive the baseline side price from the exact selected quote; modeled price is separate. The runner needs no additional plumbing and lifecycle authority is unchanged. Caller-supplied sensitivity returns immutable outcomes in canonical ascending bps order, without economic evaluation. Model v2 records the materially changed price semantics; [the data contract](DATA_CONTRACTS.md#p05b-deterministic-adverse-slippage) defines Decimal rounding and numeric-domain limits.

`shadow.execution.economics` is a separate downstream P0.5C boundary identified by `shadow.execution.fixed_quantity_fee_bps.v1`. It consumes an already-resolved `ExecutionOutcome` and can attach immutable `EconomicExecution` evidence only when that outcome is `filled`. The caller declares one fixed positive Decimal quantity, a research quote-currency denomination, and an explicit nonnegative Decimal fee in basis points. Quantity never participates in strategy sizing, risk, quote selection, fill validity, slippage, retries, or lifecycle state. The current multiplier is exactly one and has no configurable contract.

P0.5C calculates signed notional from the final modeled execution price after slippage, uses its absolute value as the fee base, and derives one execution cash-flow value. It performs no extra spread or slippage deduction. This cash-flow value is evidence for one fill, not a cash balance, settlement ledger, portfolio value, or P&L. Zero and negative execution-price outputs retain P0.5B's stress-domain semantics; the fee remains nonnegative, and those results do not establish ordinary market feasibility. Runner v3 attaches exactly one economic execution to every fill when economics is enabled and none to unfilled/rejected outcomes. P0.5 is complete under this bounded execution-evidence definition, without claiming partial fills, liquidity, impact, broker schedules, or complete market microstructure realism.

## Implemented P1A evaluation boundary

`shadow.evaluation` is a read-only downstream boundary for one supplied `SimulationResult` with P0.5C economics enabled. It never reruns the strategy, lifecycle, quote selection, or execution model. `reconstruct_trades` replays canonical event and lifecycle evidence, accounts for every attempt/outcome/economic execution, validates the complete action-attempt-position-quote chain for each fill, and pairs only one authoritative entry with the exit of that same simulated position. It rejects duplicate, omitted, stale, future, mismatched-model, quantity-mismatched, currency-mismatched, or impossible evidence. Pending/open end state is returned as incomplete evidence rather than being closed or aggregated.

`TradeRecord` exposes a completed long trade's preserved fill evidence, gross result, synthetic fees, net result, and returns when both execution prices are positive. A zero or negative execution price is a `stress_price` trade: its economics are retained, but it has no ordinary return and is excluded from ordinary totals. `HistoricalEvaluationResult` produces descriptive, currency-separated ordinary summaries only; it contains no funded cash, portfolio, position sizing, FX, compounding, drawdown, or risk-adjusted performance layer.

`ExperimentManifest` is immutable, deterministic identity for the supplied experiment. It binds bar dataset identity; the quote and opportunity evidence multisets; strategy, execution, and economics configurations; feature and simulation versions; the complete supplied simulation evidence; caller-provided code revision; and mandatory declared limitations. It identifies but does not authenticate caller-supplied evidence or revision, and deliberately performs no ambient Git, network, or filesystem lookup.

## Implemented P2A paper risk authority

`shadow.risk` introduces the first operational authorization boundary without changing the research simulator. The authority sequence is `Signal` → provider-neutral `OrderIntent` → pure `RiskDecision` → atomic `RiskGate` admission/reservation → one gate-issued `AuthorizedOrder` → one application claim. External paper dispatch remains unimplemented and requires additional submission-time revalidation. `Signal`, `LifecycleAction`, `EconomicExecution`, raw `OrderIntent`, and historical `RiskDecision` values cannot cross the supported claim seam.

The v1 intent is paper-only market/DAY, BUY entry or SELL full exit, with a separate operator-selected whole-unit quantity configuration. The immutable policy and supplied state make allowlists, quantity/concurrent-position limits, freshness, complete inventory, positions, outstanding orders, trading enablement, and kill-switch state explicit. Evaluation uses only the supplied decision time and causally available signal, matching feature, quote, state, and controls. Missing, stale, future, crossed, nonpositive, unsupported, inconsistent, or capacity-violating evidence rejects.

Business identity binds stable scope and source signal/feature semantics while excluding retry time and operational quantity. Full payload identity is separate, so exact redelivery becomes a referenced duplicate and changed content under one business identity becomes a conflict. The process-local gate records the first decision, adds existing reservations to later effective state, reserves before returning a grant, never expires capacity implicitly, and lets the future dispatch boundary claim a genuine grant once.

Risk model v2 verifies the signal's claimed threshold/reason and source-time freshness as well as feature lineage. Reservation merging deduplicates only complete equality; conflicting reference/content remains visible and rejects. First rejections remain terminal even after state recovery. Policy is fixed for the gate lifetime. Claims have no expiry and do not revalidate current controls, policy, or evidence freshness, so an admission grant alone is insufficient for external submission. Claims and reported fills never release reservations; this is a bounded admission component, not a continuous trading lifecycle. See [the risk contract](RISK_MODEL.md) for the exact operational limits.

This boundary assumes one gate owner per operational scope. It adds no broker port or adapter, order submission, persistence, distributed lock, restart reconciliation, fill handling, cancellation, portfolio accounting, or account-risk model. A newly constructed gate is not evidence that broker inventory is flat. P2A's disabled/kill-switch behavior freezes entry and exit automation; it is not a final emergency-liquidation design.

## Implemented P4A Alpaca live-data shadow

`shadow.adapters.alpaca` is an application-edge, market-data-only adapter using the documented JSON WebSocket protocol and the small `websockets` dependency. It connects only to `wss://stream.data.alpaca.markets/v2/{iex|sip}`, authenticates, subscribes to explicit symbols' `bars` and `quotes` channels, verifies the complete subscription acknowledgement, and reconnects only after transport loss. It has no trading endpoint, trading client, account endpoint, or order path.

The adapter translates JSON immediately: Alpaca minute-bar timestamps are left edges, so the SHAD0W `Bar.observation_time` is one minute later; a bar that has not ended by application receipt is rejected. Quote timestamps remain quote observation times. Both use the injected application receipt instant as `availability_time` with `SYSTEM_RECEIVED`; source timestamps after receipt, naive timestamps, malformed numerics, and unsupported symbols are rejected. Core packages retain no Alpaca knowledge.

`ShadowSession` is a bounded append-only application composition, not the historical simulator. Bars are accepted in receive order per symbol and compute the existing deterministic z-score/signal pipeline once. Exact redelivery is retained as duplicate evidence without another decision; material same-time variants and older observations are retained but excluded from forward state. Per-symbol bar freshness determines strategy readiness; absence or staleness of a quote does not invalidate bar accumulation. A quote is required only when a candidate is observed for risk evidence. A session is `starting`, `healthy`, `stale`, `disconnected`, `failed`, or `stopped`; no stale or disconnected state can imply current operational actionability.

For bounded cold-start reduction, the optional `--bootstrap-capture` composes an already verified stopped P4A capture into `ShadowSession` through a dedicated seed boundary. It validates source/configuration/scope lineage and retains the trailing valid completed one-minute bars needed to form the next feature window. This is historical rolling context, not current market evidence: seed age does not use `maximum_bar_age`, and the feature kernel has no contiguous wall-clock-window rule. It neither replays those bars through the new session clock nor evaluates strategy/risk on them. The seed is explicit, digest-identified, and replayable in the new capture; the first fresh new live bar remains causally responsible for any resulting ready feature or candidate. Bootstrap quotes, broker state, admission, and dispatch are intentionally outside this boundary.

A candidate may create an `OrderIntent` as an observational proposal, never an order. The live session evaluates the current strategy with `PositionState.FLAT` only: this is live flat-state candidate observation, so it can observe entry candidates but cannot infer a broker holding or create lifecycle-backed exits. With no broker/account adapter, the normal live command records `operational_state_unavailable` rather than asserting a flat/reconciled account or emitting a risk decision. Tests/offline replay may supply explicitly labelled non-authoritative state and use only the pure `evaluate_risk` function; P4A never imports or calls the admission gate, never creates an authorization capability, and never dispatches. `shadow.live.v1` JSONL recovers normalized inputs separately from derived-output verification: complete terminal captures are deterministically recomputed against persisted records, valid interrupted prefixes remain explicitly incomplete, and corrupt interior evidence fails closed.

## Implemented bounded P5A integration components

Current HEAD includes provider-neutral broker contracts and fake evidence, a
PAPER-only Alpaca adapter, durable SQLite journal/ownership, source-opportunity
binding, deterministic client IDs, read-only PAPER preparation, and a guarded
human-armed one-shot PAPER dispatch probe. The probe commits its attempt before
one submit call, preserves uncertainty, and never automatically resubmits.

These components do not establish P5A.3 reconciliation or lifecycle authority.
The probe's limited order lookup/snapshot checks cannot establish broker-authoritative
holdings, release capacity, recover uncertain exposure, or provide continuous entry/
exit operation. P4A therefore remains FLAT observational candidate generation.
Live-capital support remains prohibited. See [STATUS.md](STATUS.md) for current
coverage and [the execution plan](P5A_EXECUTION_PLAN.md) for the remaining
dependency order.
