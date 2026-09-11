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
- **P0.5A — deterministic execution and fill semantics: COMPLETE.** P0.4A opportunities now authorize attempts rather than state changes. Immutable quote-side attempts/outcomes use only causally available, fresh P0.1 `Quote` evidence; long entries use ask, long exits use bid, crossed quotes reject, and only `filled` outcomes transition lifecycle state. This makes quoted spread observable without adding a second spread charge, but adds no P&L, quantity, fees, slippage, liquidity model, or broker behavior.
- **P0.5B — deterministic adverse slippage sensitivity: COMPLETE.** Explicit finite nonnegative Decimal bps worsen ask-derived BUY and bid-derived SELL prices after baseline fill validation. Immutable outcomes preserve quote/configuration/baseline/modeled evidence; caller scenarios are canonical and monotonic. Zero reproduces P0.5A prices. No quantity, fees, market impact, partial fills, or evaluation was added.
- **P0.5C — fixed quantity and fee economics: COMPLETE.** A separate immutable boundary attaches one caller-declared positive Decimal quantity and one explicit nonnegative synthetic fee in bps of absolute final executed notional to each filled outcome. Fees use the final slipped price; unfilled/rejected outcomes produce no economics. Per-instrument runner configuration is complete and canonical, while quantity/currency/fees have no strategy, risk, fill, price, retry, or lifecycle authority. Individual execution cash flow adds no portfolio/P&L state.
- **P0.5 — bounded execution economics: COMPLETE.** The completed scope is quote-side executable pricing, deterministic adverse slippage, fixed declared quantity, and proportional fee evidence. It is not complete market microstructure realism: liquidity, capacity, partial fills, impact, broker schedules, and portfolio evaluation remain absent.

## P1 — evaluation

**P1A — trade reconstruction and historical-evaluation foundation: COMPLETE.** Immutable evaluation replays the canonical lifecycle trace without rerunning strategy or execution selection, pairs only matching filled entry/exit economic executions, retains unsuccessful and incomplete evidence, and fails closed on inconsistent causal, configuration, and accounting references. Completed ordinary trades expose gross/net results and returns; nonpositive-price outcomes remain explicit stress evidence outside ordinary aggregates. Currency-separated descriptive summaries never mix caller-declared denominations. An immutable manifest identifies the supplied bar, quote, opportunity, configuration, implementation, complete simulation evidence, caller-supplied code revision, and declared limitations. P1A adds no funded portfolio, compounding, FX, holdout, walk-forward, parameter-selection, benchmark, or profitability claim. No next P1 slice is authorized.

## P2 — operational risk authority

- **P2A — minimal deterministic paper risk authority: COMPLETE.** Provider-neutral paper market/DAY intents retain source signals and operator-declared whole-unit quantity. A pure versioned risk policy evaluates explicit causal feature, quote, inventory/order, and operator-control evidence. A single-process gate makes first decisions authoritative, reserves instrument/capacity before exposing one grant, rejects exact duplicates and identity conflicts, and permits one dispatch claim only from a genuine gate-issued artifact. The scope excludes persistence, broker/account reconciliation, cash, buying power, P&L, portfolio accounting, and external submission.

## P3–P9 — evidence-gated directions

- **P3:** deterministic regime classification is deferred pending operational evidence.
- **P4A — Alpaca live-data shadow: COMPLETE.** The market-data-only WebSocket adapter translates explicit IEX/SIP subscriptions directly to provider-neutral minute `Bar`/`Quote` values. Bar left-edge timestamps become interval ends and application receipt becomes `SYSTEM_RECEIVED` availability. Bounded append-only sessions retain accepted, duplicate, same-time-variant, delayed, and invalid dispositions; completed bars drive deterministic feature/signal candidates per symbol, while quote readiness is required only for a candidate's risk observation. No broker/account state is fabricated, no gate is used, no authorization or order is emitted, and normalized captures replay deterministically.
- **P5A:** Alpaca paper execution remains separately gated. It requires submission-time control/policy/freshness revalidation, durable duplicate handling, broker-authoritative account/order/position reconciliation, and a safe reservation lifecycle. P2A claims alone are insufficient for external submission and reservations do not support continuous operation.
- **P6:** asynchronous semantic event-risk classification with typed, time-bounded outputs.
- **P7:** execution-quality and signal-decay research.
- **P8:** additional strategy research.
- **P9:** future live-capital gate, only after separately defined operational, risk, and evidence requirements are met.

Later phases remain deliberately undesigned until a concrete requirement and supporting evidence justify them.
