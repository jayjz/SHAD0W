# SHAD0W implementation status

This file is the canonical current-HEAD capability snapshot.

Contracts, ADRs, dated reviews, and evidence notes retain design/history. When they conflict with this file, verify behavior against code and executable tests and treat this file as the documentation status authority.

## Summary

SHAD0W currently has two mature foundations and one bounded operational integration:

1. deterministic historical research/evaluation;
2. live and replayable market-data evidence;
3. a constrained BTC/USD Alpaca PAPER session with strict risk, journal, dispatch, and reconciliation boundaries.

The BTC path is **not** continuous trading. It permits at most one entry and one linked exit in a bounded session. Real-provider crypto fee linkage/finality remains insufficient for strict automatic exit proof, and live-capital trading is unsupported.

## IMPLEMENTED

### Deterministic research

- Provider-neutral market-data contracts with explicit observation/availability semantics.
- Strict validation, provenance, canonical serialization, and deterministic dataset identity.
- Deterministic feature kernel with explicit warm-up/unavailable states.
- Explicit strategy hypotheses that propose signals without execution authority.
- Chronological simulation with anti-look-ahead eligibility.
- Deterministic quote-side execution, adverse slippage sensitivity, fixed quantity, and synthetic fee evidence.
- Manifest-bound trade reconstruction and descriptive historical evaluation.
- Sealed temporal study evidence with immutable train/development/final partition identity.

### Independent risk authority

- Provider-neutral order intents and deterministic risk decisions.
- Fail-closed validation of causal feature, quote, inventory, order, and operator-control evidence.
- Atomic admission/reservation semantics and one-use process-local claims for the original paper-risk boundary.
- Separate BTC cash-limited risk evaluation that recomputes strategy/lifecycle evidence and applies entry/exit constraints.

### Live market-data evidence

- Alpaca IEX/SIP shadow market-data path.
- Provider-neutral BTC/USD and ETH/USD crypto trade/quote/book contracts.
- Bounded Alpaca crypto WebSocket capture and normalization.
- Localhost-only crypto relay via `shadow-crypto-feed-relay`.
- Hash-chained market evidence.
- Bounded raw BTC trade-history warm start.
- Explicit historical/live provenance boundary.
- Read-only timing evidence for relay/consumer receipt inspection.

### Durable PAPER foundations

- Typed provider-neutral broker account/asset/order/fill/position/error contracts.
- PAPER-only Alpaca adapter with bounded reads and sanitized errors.
- Durable SQLite execution journal.
- Deterministic PAPER client-order identities.
- Exclusive local account ownership bound to a journal path.
- Journal-before-dispatch ordering.
- One POST per committed attempt; no automatic retry on uncertainty.
- Bounded order-history and crypto-activity collection.
- Broker-authoritative reconciliation reducer with explicit `FLAT`, `ENTRY_PENDING`, `HOLDING`, `EXIT_PENDING`, `UNRESOLVED`, and `HALTED` states.
- Strict fractional BTC inventory and available-quantity checks.
- Activity-window filtering that preserves the requested evidence cut while keeping malformed timestamps fail-closed.

### BTC PAPER experiment and session

- `shadow-crypto-paper --preflight` read-only broker/risk/reconciliation preflight.
- Guarded initial BTC PAPER experiment path.
- `shadow-btc-paper-session` bounded strategy session.
- Repeated hourly strategy decisions and durable abstention evidence.
- One BUY maximum and one linked SELL maximum per session.
- Separate explicitly marked PAPER plumbing-probe mode.
- Fresh broker/risk revalidation before dispatch.
- Bounded duration with immutable session binding.
- Restart gates that do not reset spent attempts or extend the original session deadline.
- Kill-switch observation.
- Strategy and probe identities kept separate.

Fake-broker coverage exercises bounded round-trip and restart behavior. Real-provider validation has established live reads and accepted BTC PAPER entry behavior, but this is not proof that automatic exit can always complete.

## PARTIALLY IMPLEMENTED / EXTERNALLY BLOCKED

### Real-provider BTC exit proof

The strict reducer requires net inventory and fee effects to be established from broker evidence before authorizing the linked SELL.

Alpaca's available legacy crypto activity evidence does not currently prove the fee linkage/finality semantics SHAD0W requires. A real accepted BUY may therefore reconcile to net BTC exposure while the linked SELL remains blocked.

This is an evidence limitation, not permission to substitute gross fill quantity, guess fee completion, or relax causal/reconciliation invariants.

### Original P5A acceptance sequence

The repository contains substantial P5A foundations and a bounded BTC-specific operational path, but the original generic P5A.6–P5A.8 continuous application/recovery acceptance sequence is not complete.

The original plan remains useful as an acceptance/design record; it is not the current implementation dashboard.

## NOT IMPLEMENTED

- Continuous repeated BTC PAPER trading.
- Generic continuous lifecycle-backed PAPER application.
- Proof-grade provider fee linkage/finality for automatic BTC exits.
- Automatic retry of uncertain submissions.
- Automatic cancellation or forced liquidation at timeout.
- Live-capital endpoints, credentials, or trading support.
- A validated or profitable strategy claim.
- Portfolio optimization, leverage, or dynamic sizing.
- LLM execution authority.

## Current documentation roles

| Document | Authority |
| --- | --- |
| `README.md` | Public overview and concise current-state summary |
| `docs/STATUS.md` | Canonical documentation snapshot of current HEAD |
| `docs/ARCHITECTURE.md` | Current component, evidence, and authority boundaries |
| `docs/RISK_MODEL.md` | Current risk/dispatch authority model and limitations |
| `docs/BTC_TREND_PAPER_CONTRACT.md` | BTC strategy and bounded PAPER contract |
| `docs/BTC_EXECUTION_SEAM.md` | BTC execution/accounting evidence boundary |
| `docs/BTC_PAPER_SESSION.md` | Current bounded session behavior and operator contract |
| `docs/PROJECT_STRATEGY_AND_ENGINEERING_ROADMAP.md` | Milestone history and next evidence gates |
| `docs/P5A_EXECUTION_PLAN.md` | Original detailed P5A dependency/acceptance plan |
| ADRs / dated evidence | Historical decisions and experiment evidence |

## Status rule

Do not upgrade a capability because a happy-path test exists.

A capability is described as implemented only when the corresponding code path and executable invariants exist. Provider acceptance, continuous operation, strategy validity, profitability, and live-capital safety require separate evidence and are stated separately.
