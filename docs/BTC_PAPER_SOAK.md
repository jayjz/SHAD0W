# BTC PAPER_SOAK behavior experiment

`shadow-btc-paper-loop --paper-soak` is a separate experimental application for
bounded autonomous strategy observation on Alpaca PAPER. It supports successive
BUY/SELL cycles, but is **not proof-grade accounting**, canonical-strategy
profitability evidence, generic P5A recovery acceptance, or live-capital trading.
This change has synthetic validation only; no Alpaca execution was performed as
part of implementation.

## Authority boundary

The existing `--session`, plumbing probe, proof risk evaluator, BTC dispatcher,
reconciliation reducer, `CryptoActivityEvidence`, and `BtcJournal.usable` are
unchanged. A proof-grade BUY with missing linked fee finality remains unresolved.
PAPER_SOAK cannot adopt that position or its journal. In particular,
`btc-probe-20260927T002431Z` is neither migrated nor reused.

The soak has its own independent risk gate and operational state validator.
`PAPER_SOAK_OPERATIONAL` is persisted on binding, risk, attempt, decision and
operational-authority records. Soak events occupy `btc-paper-soak-v1` in the owned
SQLite journal. They never populate proof attempts, proof reconciliations,
`fee_complete_ids`, or `coverage_reference`. Provider activity observations are
stored exactly as returned, including missing finality. No position difference
is converted to a `CryptoFeeActivity`, fee amount, or proof inventory conclusion.

The shared components are typed PAPER requests, the fixed-origin Alpaca PAPER
adapter, local account ownership, durable application-event transactions, client
identity derivation, hash-anchored raw trade history, feature computation and
strategy proposal machinery. The separate risk gate recomputes both operational
state and strategy proposal before allowing transport; the proposal cannot grant
itself operational authority. Another risk/control/freshness check runs inside
the adapter's callback immediately before POST. There is no retry after a spent
commit, including a failure before the transport callback.

## Operational lifecycle and bounds

An initial account-wide snapshot must show no positions, outstanding orders,
orders or executions in the new run's requested history. Foreign state is not
adopted. Thereafter every observed order must match exactly one durable run
request, client identity and accepted broker order ID. Duplicate identities,
replacement chains, unknown or uncertain submission, malformed or incomplete
broker evidence, mismatched account/scope, history rewrites and position conflicts
fail closed. Missing fill activity for a completed order yields WAITING_FILLS;
outstanding orders yield PENDING. Both continue observation without submission.

Operational HOLDING requires a fully filled BUY and unique matching execution
quantities, together with a fresh positive BTC position no larger than that BUY
and explicit fresh `qty_available` equal to the position. Independent risk checks
the exact sellable amount against the current provider grid and limits. It never
rounds or substitutes gross BUY quantity. Position/availability equality is only
PAPER sellable-quantity authority; it does not identify or price fees.

After a fully filled SELL, matching executions and an empty complete account
position snapshot establish operational FLAT. Only then may the next cycle BUY.
A residual position halts. A rejected/canceled/expired order also halts rather
than consuming unlimited fresh attempts. Limits are:

- One BTC position and one pending order maximum; no pyramiding or other positions.
- At most one committed BUY and one committed SELL per cycle.
- Explicit maximum completed cycles, 1–24; no more than twice that many attempts.
- Explicit duration, 1–86,400 integer seconds, including historical warm-start.
- Explicit entry quantity and cash buffer; configurable entry notional capped at
  $100 at the fresh ask. A MARKET fill can exceed its pre-send notional estimate.
- Broker/quote/control freshness bounded at 30 seconds by the CLI policy, with a
  final send callback limited to five seconds and the remaining run duration.

A five-second heartbeat permits broker polling and kill-switch observation even
when the live socket has no trades or quotes. Polls also occur at startup, every
hourly decision, before and after a submission, and on normal termination.
Every submission requires current controls; a kill switch freezes SELL as well
as BUY. Timeout does not force an exit. A bounded synchronous provider call may
finish after the deadline, but no subsequent POST can be authorized after expiry.

Each launch requires fresh, distinct artifact paths. The immutable binding
records profile, risk policy, code revision, run identity, quantity, cycle bound,
start and deadline. Restart is deliberately **not supported for submission**:
existing evidence is rejected before appending a new soak binding. A crash leaves
its commit and raw observations for audit, and cannot restore the attempt budget.
Use the same account ownership location as other SHAD0W execution processes; its
journal binding is not automatically reset. A pre-existing bound journal or
unresolved account must be dealt with through a separately assigned operational
procedure, not a new soak scope or alternate lock directory.

## Experimental strategy profile

`paper_soak_canary()` is a distinct configuration ID using the same
`shadow.btc-trend.v1` implementation:

| Parameter | Canonical engineering | PAPER soak canary |
| --- | --- | --- |
| Interval | 1 hour | 1 hour |
| Slow trend | 72 hours | 3 hours |
| Fast momentum | 6 hours | 1 hour |
| Volatility returns | 24 hours | 2 hours |
| Minimum completed closes | 73 | 3 |

Three closes are the smallest window retaining a slower trend than momentum and
two returns for a non-degenerate population-volatility calculation. This is an
execution/behavior canary, chosen to use a short clean recent suffix rather than
wait for an older provider-empty hour to age out. The actual suffix is still
validated at runtime; no history is invented, forward-filled or repaired.
Volatility cap, trailing multiple, fee/spread/slippage assumptions, 0.009 entry
hurdle and evidence age are unchanged. No parameter tuning promises ENTER signals.
Only trades close intervals; a newly captured whole hourly interval is required
for each live decision. Warm-start closes cannot themselves submit orders.

## Evidence and summary

Raw trades are hash-anchored by `MarketEvidence`. The soak journal additionally
records every completed interval, features, individual signal filters, proposals,
risk reasons and inputs, quotes used for risk, committed requests, submission
results, original broker snapshots/activities, positions and available quantity,
cycle number, operational authority, abstentions and terminal reason. Entry/exit
execution timestamps and prices remain in broker fills. Artifacts are local and
account-bound; credentials and arbitrary provider exception text are not logged.

The terminal JSON reports duration, hourly decisions, ENTER/EXIT signals,
abstentions by reason, completed/unresolved cycles, maximum observed BTC exposure,
broker/API error count, terminal operational state and halt reason. A completed
cycle reports observed BUY and SELL VWAPs, quantities and timestamps. Gross
realized PAPER P&L is sold quantity times (SELL VWAP minus BUY VWAP), using only
observed matching fills. Any unsold BUY quantity difference remains unvalued;
this is not a net cash/inventory reconciliation or an inferred fee deduction.
Observed fee rows retain their provider asset/link fields (including unknowns).
The report always labels fee accounting INCOMPLETE; it makes no finality claim.
A timeout can leave an unresolved holding, pending order or uncertain attempt.

## Operator command (not executed during implementation)

Use PAPER credentials in `ALPACA_PAPER_API_KEY_ID` /
`ALPACA_PAPER_API_SECRET_KEY` and market credentials in `ALPACA_DATA_KEY` /
`ALPACA_DATA_SECRET`. The PAPER origin is a required exact choice; no live-money
origin or endpoint override is supported. A selected localhost relay has no
direct-provider fallback. Choose a legal BTC quantity from current asset/quote
evidence. Fresh paths must have existing parent directories.

```sh
uv run shadow-btc-paper-loop --paper-soak \
  --paper-endpoint https://paper-api.alpaca.markets \
  --paper-acknowledgement I-UNDERSTAND-BOUNDED-BTC-PAPER-SOAK \
  --account-id '<FLAT_PAPER_ACCOUNT>' --operational-scope '<NEW_SOAK_SCOPE>' \
  --run-id '<NEW_SOAK_RUN>' --code-revision '<COMMIT_SHA>' \
  --quantity '<LEGAL_BTC_QUANTITY>' --maximum-entry-notional 100 --cash-buffer 10 \
  --maximum-cycles 2 --duration-seconds 14400 --trading-enabled \
  --kill-switch-path '<ARTIFACT_DIR>/STOP' \
  --journal-path '<ARTIFACT_DIR>/soak.sqlite' \
  --market-evidence-path '<ARTIFACT_DIR>/soak-market.jsonl' \
  --experiment-evidence-path '<ARTIFACT_DIR>/soak-summary.json' \
  --ownership-directory '<ACCOUNT_OWNERSHIP_DIR>' \
  --market-data-relay ws://127.0.0.1:8766
```

12h and 24h bounds use 43,200 and 86,400 seconds respectively. The operator must
not launch this against the unresolved probe account state to bypass proof gates.
