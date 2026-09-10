# SHAD0W strategy and engineering roadmap

## Governing rule

SHAD0W advances only when evidence justifies the next gate. A phase is not a commitment to build later phases, nor does a profitable backtest establish a durable edge. As implementation appears, the documented invariants below must acquire executable regression or property tests.

## P0 — research foundation

- **P0.0 — repository operating foundation: COMPLETE.** Established the operating contract, scientific method, boundaries, and bootstrap tooling.
- **P0.1 — deterministic market-data contracts: COMPLETE.** Provider-neutral time-aware data types, strict validation, provenance, deterministic SHA-256 identity, and local fixtures have executable temporal and validation invariants.
- **P0.2 — deterministic feature engine: NEXT.** Calculate features from validated chronological data with explicit availability semantics and reproducibility tests.
- **P0.3 — minimal mean-reversion baseline:** specify one falsifiable short-horizon hypothesis and its configuration; signal generation remains distinct from risk.
- **P0.4 — chronological/event-driven simulator:** model decision and fill timing without same-bar leakage; make timeline behavior executable.
- **P0.5 — transaction-cost/fill model:** evaluate explicit spread, slippage, latency, rejected-fill, and execution-constraint assumptions.

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
