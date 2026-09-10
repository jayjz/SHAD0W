# SHAD0W strategy and engineering roadmap

## Governing rule

SHAD0W advances only when evidence justifies the next gate. A phase is not a commitment to build later phases, nor does a profitable backtest establish a durable edge. As implementation appears, the documented invariants below must acquire executable regression or property tests.

## P0 — research foundation

- **P0.0 — repository operating foundation: COMPLETE.** Established the operating contract, scientific method, boundaries, and bootstrap tooling.
- **P0.1 — deterministic market-data contracts: COMPLETE.** Provider-neutral time-aware data types, strict validation, provenance, deterministic SHA-256 identity, and local fixtures have executable temporal and validation invariants.
- **P0.2A — deterministic feature kernel: COMPLETE.** Immutable close-price return and strict rolling-statistic snapshots from P0.1-validated bars, explicit warm-up/unavailability semantics, propagated availability, fixed Decimal arithmetic, and prefix-stability regression tests.
- **P0.2B — strategy-facing indicators and feature composition: COMPLETE (no additional code required).** The first hypothesis composes and thresholds P0.2A close z-score evidence: `price < mean - k * std` is representable as `z_score < -k`. No redundant Bollinger-band wrapper, RSI, or composition framework was added.
- **P0.3 — minimal mean-reversion baseline: COMPLETE.** One immutable-configured, provider-neutral, signal-only close z-score hypothesis proposes long entry or exit evidence. It is unvalidated and makes no profitability claim; risk remains independent.
- **P0.4A — chronological event semantics: COMPLETE.** Immutable availability/event evidence, canonical event ordering, and strict later-opportunity eligibility prevent same-bar completed-close execution without creating fills or portfolio state.
- **P0.4B — authoritative position/order lifecycle: COMPLETE.** Immutable per-instrument `flat`/`pending_entry`/`holding`/`pending_exit` state, deterministic conflict policy, strict P0.4A eligibility composition, unresolved-state reporting, and reconstructable transition evidence exist without prices, quantities, costs, P&L, or broker behavior.
- **P0.4C — end-to-end chronological simulator: COMPLETE.** One immutable result now composes P0.1 validated bars, P0.2 availability-aware z-score snapshots, P0.3 proposals, P0.4A canonical eligibility, and P0.4B authoritative per-instrument lifecycle state. Decisions occur once per feature availability instant, pending state fails closed, prefix evidence is regression-tested, and no economic execution semantics are claimed.
- **P0.5 — transaction-cost/fill model: NEXT.** Evaluate explicit spread, slippage, latency, rejected-fill, and execution-constraint assumptions without treating P0.4C opportunities as guaranteed fills.

## P1 — evaluation

Build metrics, temporal development/validation/final-holdout separation, walk-forward evaluation, parameter-stability analysis, and ablation testing. Advancement requires evidence robust to costs, reasonable execution assumptions, and known limitations.

## P2–P9 — evidence-gated directions

- **P2:** independent deterministic risk engine.
- **P3:** deterministic regime classification.
- **P4:** live-data shadow mode with no orders.
- **P5:** Alpaca paper execution behind ports/adapters.
- **P6:** asynchronous semantic event-risk classification with typed, time-bounded outputs.
- **P7:** execution-quality and signal-decay research.
- **P8:** additional strategy research.
- **P9:** future live-capital gate, only after separately defined operational, risk, and evidence requirements are met.

Later phases remain deliberately undesigned until a concrete requirement and supporting evidence justify them.
