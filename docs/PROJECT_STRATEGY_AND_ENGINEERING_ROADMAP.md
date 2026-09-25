# SHAD0W strategy and engineering roadmap

## Governing rule

SHAD0W advances only when evidence justifies the next authority boundary. A completed implementation milestone does not imply strategy validity, provider acceptance, continuous operation, or profitability.

For the current implementation snapshot, use [STATUS.md](STATUS.md). This document records milestone history and the next evidence gates.

## Completed research foundation

### P0 — deterministic research foundation

Completed:

- provider-neutral market-data contracts and provenance;
- deterministic dataset identity;
- deterministic feature kernel;
- explicit mean-reversion research hypothesis;
- chronological event semantics;
- simulated lifecycle;
- end-to-end deterministic simulation;
- quote-side execution semantics;
- adverse slippage sensitivity;
- fixed quantity and synthetic fee economics.

### P1 — evaluation and study evidence

Completed:

- manifest-bound trade reconstruction;
- descriptive historical evaluation;
- content-addressed frozen bar datasets;
- predeclared train/development/final study partitions;
- immutable final release evidence.

These capabilities preserve research evidence. They do not establish a profitable edge.

### P2 — deterministic risk foundation

Completed:

- provider-neutral paper intents;
- pure fail-closed risk evaluation;
- duplicate/identity rules;
- atomic in-process admission/reservation;
- one-use process-local claim semantics.

P2A remains a pure authority model rather than restart-safe broker execution.

### P4 — live market-data shadow

Completed:

- Alpaca IEX/SIP shadow capture;
- deterministic normalized evidence;
- warm-start support;
- observational candidates without broker authority.

## Current P5 / BTC operational work

The original P5A plan described the dependency chain for durable PAPER execution. HEAD now contains more of that foundation than the original phase labels imply.

Implemented components include:

- typed broker evidence contracts and fake broker;
- SQLite journal and local account ownership;
- deterministic PAPER client IDs;
- bounded order-history collection;
- Alpaca PAPER adapter;
- journal-before-dispatch guard;
- one-POST attempt semantics;
- broker-authoritative reconciliation reducer;
- typed BTC net fee/inventory accounting;
- BTC-specific durable authority/counters;
- raw BTC history warm start;
- local BTC market-data relay;
- BTC trend/momentum/volatility strategy candidate;
- read-only PAPER preflight;
- guarded initial BTC experiment;
- bounded BTC strategy session with repeated hourly decisions;
- at most one entry plus one linked exit.

This is meaningful operational infrastructure, but it is **not continuous trading**.

## Current evidence gap

The highest-value unresolved operational problem is real-provider proof for the linked BTC exit.

The strict accounting model needs evidence sufficient to establish:

1. entry execution;
2. fee effects;
3. net broker position;
4. currently available BTC;
5. safe exact exit quantity.

Current provider activity semantics do not prove fee linkage/finality strongly enough for SHAD0W to automatically upgrade every real BUY into SELL authority.

Until that is solved, the correct behavior is to fail closed.

## Near-term gates

### Gate A — finish bounded real-provider lifecycle evidence

Goal: demonstrate or explicitly bound the evidence needed for BUY → reconciled HOLDING → linked SELL → FLAT without weakening accounting rules.

Success requires provider-backed evidence, not a forced demonstration trade.

### Gate B — recovery acceptance

Exercise durable recovery across:

- crash before/after commit;
- accepted submission with lost response;
- duplicate delivery;
- restart with exposure;
- delayed or corrected activity;
- ownership/journal conflicts;
- unresolved fee state.

No automatic retry may appear as a shortcut.

### Gate C — continuous PAPER application

Only after bounded lifecycle and recovery evidence are strong enough should SHAD0W add repeated multi-cycle PAPER operation.

A continuous application must preserve:

- broker-authoritative lifecycle;
- durable attempt budgets;
- no identity reset via new session labels;
- restart reconciliation;
- kill-switch semantics;
- unresolved exposure handoff;
- strategy/risk separation.

### Gate D — strategy evaluation

The BTC engineering configuration must be evaluated separately from operational plumbing.

Relevant questions include:

- signal frequency;
- abstention rate;
- sensitivity to cost hurdle;
- parameter stability;
- holdout behavior;
- execution friction;
- regime dependence;
- multiple-testing risk.

Operational correctness is not evidence of edge.

## Deferred directions

Deferred until justified by evidence:

- deterministic regime classification;
- asynchronous event-risk intelligence;
- execution-quality / signal-decay research;
- additional strategies;
- portfolio-level risk;
- live-capital support.

## Explicitly not on the current roadmap gate

No current milestone authorizes:

- live-capital trading;
- silent relaxation of causal or reconciliation invariants;
- retrying uncertain submissions;
- automatic forced liquidation;
- profitability claims based on the bounded PAPER canary.
