# SHAD0W implementation status

This is the canonical current-HEAD capability snapshot. Contracts and ADRs retain
their historical/design context; see this file before treating either as a current
implementation claim.

## IMPLEMENTED

- **Bounded BTC session:** `shadow-btc-paper-session` composes repeated hourly
  decisions, durable abstention evidence, one entry plus one linked exit, and a
  separate explicitly marked broker plumbing probe. Both use existing guarded
  dispatch and strict net reconciliation; exits also require explicit broker
  available BTC. Fake-broker tests cover strategy and probe round trips and
  restart gates. Real Alpaca delayed fee finality remains a blocker to automatic
  exits; this does not establish continuous trading. See the
  [session contract and operator commands](BTC_PAPER_SESSION.md).

- **BTC trend candidate:** deterministic completed trade intervals, configurable
  trend/momentum/volatility features, cost-aware long/cash proposals and high-water
  reconstruction. The [engineering configuration](BTC_TREND_PAPER_CONTRACT.md) is
  unvalidated. Typed BTC MARKET/GTC transport and exact fractional reconciliation
  are tested with fake broker evidence. Independent BTC cash-limited risk
  evaluation recomputes features/lifecycle and rejects missing exit fill history.
  The [BTC execution seam](BTC_EXECUTION_SEAM.md) adds typed net fee accounting,
  bounded PAPER activity GETs, explicit SQLite v4→v5 migration, durable BTC
  authority/counters, guarded one-use dispatch and fake-broker restart tests.
  Alpaca fee linkage/finality remain unproven by the documented legacy endpoint,
  so strict proof activation fails closed. The bounded Alpaca raw BTC trade
  history path supports deterministic 73+ hourly warm-start, an explicit
  historical/live provenance boundary, and a wholly fresh live interval before
  a trigger. Journal-anchored market evidence supports durable replay. The
  bounded `shadow-crypto-paper` application composes read-only preflight and a
  single supervised initial-entry experiment: it requires an explicit
  acknowledgement and trading-enabled control, uses fresh broker/risk evidence
  immediately before the existing guarded dispatch seam, records post-attempt
  observations, and stops. Historical context alone cannot submit. The strict
  proof reducer remains unchanged: the initial experiment is allowed only at
  the documented pristine fee-finality seam. These features are not a strategy
  validation or profitability claim.

### BTC PAPER experiment application

- Deterministic raw BTC warm-start.
- Fresh-live trigger boundary.
- Experiment/proof authority distinction.
- Guarded initial experimental dispatch.
- Bounded `shadow-crypto-paper` preflight/experiment runner.

- **Crypto C0:** provider-neutral BTC/USD and ETH/USD trade/quote/book event values,
  exact UTC nanosecond times, validation tests and the
  [crypto market-data contract](CRYPTO_MARKET_DATA_CONTRACT.md).
- **Crypto C1.3:** bounded Alpaca `crypto/us` WebSocket capture for BTC/USD and
  ETH/USD trades, quotes and order books, Decimal-preserving decode, exact local
  receipt-clock evidence, reset-gated reconstruction, hash-chained JSONL, and
  clock-free replay. It has no execution, risk, or strategy authority.

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
- **Local Alpaca relays:** localhost-only market-data fanout with no trading or
  account authority. The IEX relay remains separate from the crypto relay;
  `shadow-crypto-feed-relay` is the one-owner BTC/USD crypto stream relay on
  `127.0.0.1:8766`, with bounded downstream fanout and fail-closed reconnects.
- **Warm start:** a verified stopped capture can seed the trailing valid completed
  bars needed for a feature window. Historical seeds are not live-actionable bars;
  the first fresh live bar remains the only candidate trigger.
- **P5A foundations:** provider-neutral broker contracts/fake, SQLite journal with
  local ownership, `shadow.source-opportunity.v2`, deterministic PAPER client
  order identity, and a pure broker-authoritative reconciliation reducer. The
  reducer projects `FLAT`, `ENTRY_PENDING`, `HOLDING`, `EXIT_PENDING`,
  `UNRESOLVED`, or `HALTED` from persisted attempts and complete typed broker
  evidence; it has no submission authority. Journal schema v4 fails closed on
  v3 journals. A bounded Alpaca PAPER history collector can request an explicit
  window from the earliest committed attempt and exhaust order pages; it is
  evidence collection only and is not composed into continuous PAPER trading.

## PARTIALLY IMPLEMENTED

- **Alpaca PAPER adapter and preparation:** fixed PAPER-only adapter, typed read
  evidence, and `shadow-paper-prepare` can produce a current risk decision from a
  verified P4A candidate.
- **Guarded one-shot dispatch:** the journaled dispatcher and `shadow-paper-canary`
  support a human-armed, one-share SPY BUY integration probe with one submit attempt.
  It is an early bounded supervised one-shot PAPER integration probe, not final
  P5A.8 acceptance and not continuous trading.
- **Post-attempt checks:** the probe can persist limited lookup/snapshot evidence
  and halt on uncertainty. The probe itself remains a bounded one-shot path; the
  future continuous application must consume the separate reconciliation reducer.

## DESIGNED ONLY

- **Crypto C1 measurements:** descriptive research measurements remain planned.

- **P5A.6:** continuous lifecycle-backed PAPER application.
- **P5A.7/P5A.8:** recovery acceptance and the later full-reconciliation supervised
  canary acceptance run.

## NOT IMPLEMENTED

- Real-provider acceptance of automated BTC exits.
- Continuous BTC trading.
- Proof-grade Alpaca fee finality/linkage.
- Automatic retry/recovery of uncertain submissions.
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
