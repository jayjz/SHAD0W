# Risk model

## P2A authority

`shadow.risk` is the independent deterministic admission boundary for the paper target. A strategy `Signal` remains a proposal. An `OrderIntent` adds provider-neutral operational semantics but still carries no submission authority. A historical `RiskDecision(AUTHORIZED)` is evidence only. The application dispatch seam accepts a gate-issued `AuthorizedOrder`, which can be claimed once. It proves authorization at admission time only and is insufficient for external submission.

P2A supports long equity-style BUY entry and SELL full exit using only market/DAY semantics. `OperationalQuantityConfig` is separate from research `ExecutionEconomicsConfig`: the operator declares a finite quantity, and risk requires whole positive units within the policy maximum. The strategy never sizes. Fractional-share support may be introduced later at the operational/broker layer; whole units are a P2A restriction rather than a universal SHAD0W invariant.

## Policy and evidence

`RiskPolicy` is immutable, versioned, and explicit about enabled state, allowed instruments, maximum quantity per order, maximum concurrent positions, and maximum signal, feature, quote, and operational-state ages. Construction has no trading-enabled default. P2A deliberately has no cash, equity, buying-power, notional, leverage, loss, P&L, VaR, Kelly, or dynamic-sizing rule because no authoritative account model exists.

`RiskState` carries a declared operational scope and revision, inventory completeness, known open long positions, outstanding operational orders/authorizations, observation and availability times, and timestamped operator controls. Missing inventory is never flat. Trading disabled or an active kill switch rejects both entries and exits. This is an automation freeze for P2A, not a final emergency-liquidation policy.

The pure evaluator consumes an explicit decision time and performs no wall-clock, environment, filesystem, network, broker, or global-state lookup. Signal, feature, quote, state, and controls must not be future evidence. Freshness uses observation time (signal decision time for signal age); a recent availability time cannot refresh an old observation. Age equal to the maximum is accepted; one microsecond beyond rejects. Signal fields must match the supplied ready `FeatureSnapshot`, and its claimed reason, threshold condition, and feature freshness at signal creation must be internally consistent. The quote must match the instrument, be available, be fresh, have positive sides, and not be crossed; a positive locked quote is permitted. Thus P0.5 stress-domain nonpositive prices cannot authorize an operational order.

Risk model `shadow.risk.paper.v2` adds signal-rule consistency checks and preserves conflicting reservation evidence during merging. It supersedes v1 rather than silently giving changed decisions the same implementation label. These checks establish consistency of supplied evidence, not authenticity of dataset/configuration labels or a reconstruction of features from market history. Callers must bind immutable configuration and dataset identities; changing both a signal and its supporting evidence consistently is not detectable as tampering by P2A.

## Admission, duplicates, and capacity

The business identity is a SHA-256 digest of a narrow ordered encoding of operational scope, instrument, strategy/configuration identity, signal type, and source-feature identity. Consumer/strategy-evaluation time and operational quantity are excluded. A separate payload fingerprint covers the full signal content, quantity and its configuration identity, target, market/DAY semantics, and intent time. Decimal encoding is canonical and independent of ambient context.

The first gate decision for a business identity is authoritative. An exact delivery retry returns `duplicate_intent` evidence referencing that decision and cannot issue another grant. Changed quantity, operational configuration, or content under the identity returns `intent_identity_conflict`; later state or policy evidence cannot revive the source signal.

This is deliberately a one-shot opportunity policy, including rejection for incomplete inventory, stale state, or disabled trading. Rejection does not assert that the market opportunity was executed: it closes that opportunity to this gate. Recovery can admit a new source opportunity but cannot retry the rejected one into authorization. There is no retryable/indeterminate disposition in P2A. Policy is immutable and fixed at gate construction; there is no supported policy-update API. Replacing the gate to change policy would lose history and is not a safe duplicate-handling procedure.

For entry, a known position or any outstanding order for the instrument rejects. Known positions plus BUY reservations consume the concurrent-position limit. For exit, inventory must contain one known long position and requested quantity must equal it; missing, partial, and excess exits reject. Any outstanding order for the instrument rejects. An exit reservation does not free position capacity because only future authoritative reconciliation can establish closure.

`RiskGate` holds one lock across prior-history inspection, effective-state evaluation, decision recording, reservation, and grant issuance. Rejected decisions create no reservation. Authorized decisions reserve before the artifact is exposed, and reservations do not expire. An exact complete reservation echoed in supplied outstanding state counts once. A reused reference with changed contents or the same reservation under a changed reference retains both pieces of evidence and fails closed as inconsistent state. Merging never advances observation/availability timestamps or inventory completeness. State IDs/revisions are evidence labels, not a monotonic reconciliation protocol; payload fingerprints distinguish changed contents even when labels are reused.

`claim_for_dispatch()` validates the gate-local token and recorded grant and returns the recorded intent once. It consumes no current controls, policy, clock, quote, or state. Grants have no expiry; later kill-switch activation, trading disablement, or elapsed freshness do not revoke an existing claim. Even private replacement of policy does not revoke it. Consequently a claim must never be treated as freshness-valid permission for external dispatch. P5A must design current-control/policy revalidation and bounded authorization freshness at submission, including the race between revalidation and dispatch. P2A provides no live control infrastructure.

Claims, abandoned grants, and hypothetical broker rejection do not release reservations. A reported position alongside a retained BUY reservation is inconsistent and rejects even an attempted exit; an initial known position can receive a full-exit grant, but its SELL reservation never frees capacity. P2A therefore cannot support a continuous entry/fill/exit cycle. It deliberately has no fill, rejection, cancellation, timeout, or reconciliation transition, and a caller must not infer release from a changed state snapshot.

P2A assumes exactly one gate owner for a declared scope. Its history, reservations, and token registry are in memory. Ordinary dataclass construction and reconstructed historical evidence cannot claim; Python introspection can copy a private issuer/token into an equivalent artifact, but that artifact shares the original grant's single consumption record. Another gate cannot consume it, even when deterministic grant IDs match. There is no hostile-process security: code that can mutate private gate internals is outside this guarantee. P2A provides application authority discipline, not cryptographic isolation, distributed locking, restart-safe idempotency, or evidence that a newly started process matches the brokerage account. P4A must remain shadow-only; P5A will require broker-authoritative reconciliation and durable duplicate handling, as well as submission-time revalidation, before paper submission is safe.

## P5A relationship and remaining authority requirements

HEAD includes an early bounded one-shot PAPER probe outside P2A: it uses typed
broker evidence, a journal, a PAPER adapter, and submission-time checks. It does
not change the P2A behavior above or establish P5A.3 reconciliation/lifecycle
authority. [STATUS.md](STATUS.md) is authoritative for current coverage; the
[execution contract](P5A_EXECUTION_CONTRACT.md) and
[ADR 0005](decisions/0005-paper-execution-recovery.md) retain the design rationale.
No live-capital trading is permitted.

P5A.3 and later require a versioned durable risk authority preserving P2A pure rules and
terminal decisions, with broker account/clock/asset eligibility, buying power,
current controls, freshness, canary limits and durable daily usage. Admission,
reservation, capability consumption and dispatch markers must be journaled before
external effects. Revalidation must account for its own exact reservation without
discarding any other exposure; historical authorization cannot substitute for
current checks. A bounded final-send check narrows but cannot eliminate the race
with broker state, market close or an in-flight control change.

Broker evidence alone supports reservation release or transformation, with linked
orders/fills and complete positions agreeing in a committed reconciliation revision.
Partial fills retain actual exposure and remaining obligation. Uncertain submissions
halt all new submits, retain capacity and require reconciliation; deterministic
client IDs do not authorize blind retries. Restarts rebuild durable history and
reconcile before fresh authorization, never by resetting the P2A gate.

The initial envelope is one liquid allowlisted equity, one whole share, market/DAY,
regular hours, one concurrent position, and a required daily attempt ceiling with
entry budget reserved for a later exit. Reconciled holdings support full threshold
exit proposals under the unchanged strategy. Kill switch, stale state or exhausted
budget can block exits; none means liquidation. Operator recovery and unresolved
exposure must remain explicit. [Follow-up tickets](P5A_EXECUTION_PLAN.md) define the
tests and separate canary approval needed to make these requirements executable.
