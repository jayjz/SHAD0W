# SHAD0W implementation status

This is the canonical current-HEAD capability snapshot. Contracts and ADRs retain
their historical/design context; see this file before treating either as a current
implementation claim.

## IMPLEMENTED

- **P0 research/data/features/simulation:** deterministic provider-neutral market
  contracts, feature kernel, mean-reversion hypothesis, chronological lifecycle,
  quote-side execution economics, and reproducible simulation evidence.
- **P1A evaluation:** manifest-bound trade reconstruction and descriptive historical
  summaries; no profitability conclusion.
- **P2A risk:** provider-neutral intents, pure fail-closed risk evaluation, and
  process-local atomic admission/reservation with one-use claims.
- **P4A live shadow:** Alpaca IEX/SIP market-data observation, deterministic JSONL
  capture/replay, and observational candidates evaluated with `PositionState.FLAT`.
  It neither infers broker holdings nor creates lifecycle-backed exits.
- **Local Alpaca relay:** localhost-only market-data fanout; it has no trading or
  account authority.
- **Warm start:** a verified stopped capture can seed the trailing valid completed
  bars needed for a feature window. Historical seeds are not live-actionable bars;
  the first fresh live bar remains the only candidate trigger.
- **P5A foundations:** provider-neutral broker contracts/fake, SQLite journal with
  local ownership, `shadow.source-opportunity.v2`, and deterministic PAPER client
  order identity. Journal schema v4 fails closed on v3 journals.

## PARTIALLY IMPLEMENTED

- **Alpaca PAPER adapter and preparation:** fixed PAPER-only adapter, typed read
  evidence, and `shadow-paper-prepare` can produce a current risk decision from a
  verified P4A candidate.
- **Guarded one-shot dispatch:** the journaled dispatcher and `shadow-paper-canary`
  support a human-armed, one-share SPY BUY integration probe with one submit attempt.
  It is an early bounded supervised one-shot PAPER integration probe, not final
  P5A.8 acceptance and not continuous trading.
- **Post-attempt checks:** the probe can persist limited lookup/snapshot evidence
  and halt on uncertainty. This is not broker-authoritative reconciliation or an
  operational lifecycle projection.

## DESIGNED ONLY

- **P5A.3:** broker-authoritative reconciliation and operational lifecycle
  projection.
- **P5A.6:** continuous lifecycle-backed PAPER application.
- **P5A.7/P5A.8:** recovery acceptance and the later full-reconciliation supervised
  canary acceptance run.

## NOT IMPLEMENTED

- Continuous or automatic PAPER trading, lifecycle-backed live exits, and automatic
  retry/recovery of uncertain submissions.
- Live-capital endpoints, credentials, or trading support.
- A validated/profitable strategy claim.

## Documentation roles

| Document | Role |
| --- | --- |
| `README.md` | Public purpose and concise current-state summary |
| `docs/STATUS.md` | Canonical current implementation snapshot |
| `docs/ARCHITECTURE.md` | Implemented components and authority boundaries |
| `docs/PROJECT_STRATEGY_AND_ENGINEERING_ROADMAP.md` | Completed, current, and next milestones |
| `docs/P5A_EXECUTION_PLAN.md` | Detailed P5A dependency and acceptance plan |
| `docs/P5A_CANARY_RUNBOOK.md` | Procedure for the early supervised one-shot PAPER probe only |
| ADRs and dated reviews | Historical decisions/evidence, not status dashboards |
