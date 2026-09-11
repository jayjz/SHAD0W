# Risk model

## P2A authority

`shadow.risk` is the independent deterministic authorization boundary for the paper target. A strategy `Signal` remains a proposal. An `OrderIntent` adds provider-neutral operational semantics but still carries no submission authority. A historical `RiskDecision(AUTHORIZED)` is evidence only. The supported dispatch seam accepts a live, gate-issued `AuthorizedOrder`, which can be claimed once.

P2A supports long equity-style BUY entry and SELL full exit using only market/DAY semantics. `OperationalQuantityConfig` is separate from research `ExecutionEconomicsConfig`: the operator declares a finite quantity, and risk requires whole positive units within the policy maximum. The strategy never sizes. Fractional-share support may be introduced later at the operational/broker layer; whole units are a P2A restriction rather than a universal SHAD0W invariant.

## Policy and evidence

`RiskPolicy` is immutable, versioned, and explicit about enabled state, allowed instruments, maximum quantity per order, maximum concurrent positions, and maximum signal, feature, quote, and operational-state ages. Construction has no trading-enabled default. P2A deliberately has no cash, equity, buying-power, notional, leverage, loss, P&L, VaR, Kelly, or dynamic-sizing rule because no authoritative account model exists.

`RiskState` carries a declared operational scope and revision, inventory completeness, known open long positions, outstanding operational orders/authorizations, observation and availability times, and timestamped operator controls. Missing inventory is never flat. Trading disabled or an active kill switch rejects both entries and exits. This is an automation freeze for P2A, not a final emergency-liquidation policy.

The pure evaluator consumes an explicit decision time and performs no wall-clock, environment, filesystem, network, broker, or global-state lookup. Signal, feature, quote, state, and controls must not be future evidence. Freshness uses observation time; a recent availability time cannot refresh an old observation. Signal fields must match the supplied ready `FeatureSnapshot`. The quote must match the instrument, be available, be fresh, have positive sides, and not be crossed; a positive locked quote is permitted. Thus P0.5 stress-domain nonpositive prices cannot authorize an operational order.

## Admission, duplicates, and capacity

The business identity is a SHA-256 digest of a narrow ordered encoding of operational scope, instrument, strategy/configuration identity, signal type, and source-feature identity. Consumer/strategy-evaluation time and operational quantity are excluded. A separate payload fingerprint covers the full signal content, quantity and its configuration identity, target, market/DAY semantics, and intent time. Decimal encoding is canonical and independent of ambient context.

The first gate decision for a business identity is authoritative. An exact delivery retry returns `duplicate_intent` evidence referencing that decision and cannot issue another grant. Changed quantity, operational configuration, or content under the identity returns `intent_identity_conflict`; later state or policy evidence cannot revive the source signal.

For entry, a known position or any outstanding order for the instrument rejects. Known positions plus BUY reservations consume the concurrent-position limit. For exit, inventory must contain one known long position and requested quantity must equal it; missing, partial, and excess exits reject. Any outstanding order for the instrument rejects. An exit reservation does not free position capacity because only future authoritative reconciliation can establish closure.

`RiskGate` holds one lock across prior-history inspection, effective-state evaluation, decision recording, reservation, and grant issuance. Rejected decisions create no reservation. Authorized decisions reserve before the artifact is exposed, and reservations do not expire. The one-use dispatch claim validates the gate-local token and recorded grant before returning the intent to a future paper adapter.

P2A assumes exactly one gate owner for a declared scope. Its history, reservations, and token registry are in memory. It provides application authority discipline, not cryptographic isolation, distributed locking, restart-safe idempotency, or evidence that a newly started process matches the brokerage account. P4A must remain shadow-only; P5A will require broker-authoritative reconciliation and durable duplicate handling before paper submission is safe.
