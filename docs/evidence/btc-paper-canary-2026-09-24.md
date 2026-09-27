# BTC PAPER canary evidence — 2026-09-24

## Scope

This record captures the first completed real-provider BTC PAPER experiment run for the bounded `shadow-crypto-paper` application.

This is engineering evidence, not a profitability claim, strategy validation, or authorization for live capital.

## Run identity

| Field | Value |
| --- | --- |
| Experiment code revision | `9935cfa6003b142e33a2a59918676d26bba7faaa` |
| Commit | `fix(journal): preserve reopen after application evidence` |
| Run ID | `btc-canary-004` |
| Operational scope | `btc-paper-canary-004` |
| Target | Alpaca PAPER |
| Instrument | BTC/USD |
| Quantity configured | 0.001 BTC |
| Maximum entry notional | $100 |
| Cash buffer | $10 |
| Evidence capture time | 2026-09-24T21:09:00Z |

The operator's local working branch at evidence collection time was `feat/shared-alpaca-crypto-relay`, but the executed experiment was bound to the exact code revision above. The evidence claims in this document therefore attach to that immutable revision, not to later branch work.

## Sanitized artifact hashes

The raw runtime artifacts remain outside Git.

| Artifact | SHA-256 |
| --- | --- |
| SQLite journal | `8d225ca78e1fcba16ca778b38d10f9a6bf3b36d2e0b12c0593a7dc968ea9fd42` |
| Preflight JSON | `98ea9b19621c10b33cfb035e86bce9fea27bdabb2624a16351a8a5560004ae9f` |
| Experiment JSON | `f1b7231e4e1a44fd4f2bac31c2a877df0e71944a0bd9b3717ea6a8a499d253a9` |
| Market evidence JSONL | `abec2242ced91fa61f9d6f333b45cbd79dffe01d0915de17ffb39fb1aa74c472` |
| Operator summary | `7cf412b4035f2e0f68b7d8f3365cabc828fe4407b824d75960f4268ad29f7e3a` |

No credentials, raw account identifier, raw broker payloads, SQLite database, or market JSONL are committed here.

## Preflight

Observed before the experiment:

```text
authority_classification = experiment_ready
broker_ready = true
btc_asset_ready = true
execution_authority_ready = true
experiment_blockers = []
experiment_ready = true
submission_budget = 1
proof_ready = false
proof_blocker = provider_fee_linkage_finality
strict_lifecycle = unresolved
warm_start_ready = false
fresh_live_ready = false
```

Interpretation:

- Real Alpaca PAPER read-side connectivity succeeded.
- BTC asset eligibility was established.
- The narrow initial experimental authority seam was available.
- Strict proof authority correctly remained unavailable because provider fee linkage/finality was not established.
- No trading submission occurred during preflight.

## Experiment result

The bounded experiment completed with:

```text
experiment_status = complete
final_stop_reason = NO_SIGNAL
warm_start_ready = true
fresh_live_ready = true
broker_ready = true
btc_asset_ready = true
execution_authority_ready = true
experiment_ready = true
submission_budget = 1
submission = null
client_order_id = null
observed_broker_orders = 0
observed_broker_position = null
observed_activity_executions = 0
observed_activity_fees = 0
```

This establishes that the application completed the real-provider path through historical warm-start, a wholly fresh live interval, strategy evaluation, broker observation, durable evidence, and terminal abstention without issuing an order.

The run did **not** exercise POST, fill, fee, holding, exit, or post-fill reconciliation behavior.

## Journal evidence

SQLite integrity check returned `ok`.

Observed current-version schema:

- `application_events`
- `btc_events`
- execution journal projection tables
- expected immutability/transition triggers

BTC authority event counts:

| Event | Count |
| --- | ---: |
| configure | 1 |
| reconcile | 3 |
| attempt | 0 |
| submission | 0 |

Application evidence counts:

| Namespace | Count |
| --- | ---: |
| market | 21 |
| btc-experiment | 1 |

`committed_attempts = 0`.

The absence of an `attempt` event, committed attempt row, client order ID, broker order, execution activity, or position is mutually consistent with a no-dispatch run.

## Signal analysis

### What is established

The strategy returned `NO_SIGNAL` after a fresh live interval.

The engineering configuration requires all entry conditions simultaneously:

- trend distance `D > 0`;
- six-hour fast return `M > 0`;
- hourly-return volatility `sigma <= 0.03`;
- trend distance greater than modeled round-trip cost plus safety margin.

For the current MARKET engineering canary:

```text
modeled round-trip cost = 0.007
safety margin           = 0.002
required trend distance = D > 0.009
```

The 0.9% trend-distance hurdle is deliberately conservative and is an engineering assumption, not an empirically optimized threshold.

### What is not established

The current experiment artifact records these fields as null on `NO_SIGNAL`:

- `trend_distance`
- `momentum_6h`
- `volatility`
- `modeled_cost_hurdle`
- `proposal_reason`

Therefore this run does **not** establish which entry condition failed.

It also does not prove that the 0.9% cost hurdle is too strict.

The application currently loses an important distinction because `propose()` returns `None` for both:

1. unavailable/invalid/stale feature evidence; and
2. valid feature evidence that fails one or more entry filters.

The terminal result preserves only the absence of a proposal.

## Code-path assessment

The strategy logic at the tested revision is internally consistent with its documented contract:

1. completed hourly intervals are causal and require a later trade to close;
2. feature calculation requires contiguous, available, completed intervals;
3. the 72h slow baseline, 6h momentum and 24h population-return volatility match the documented implementation;
4. entry requires every configured filter to pass;
5. risk independently recomputes features and the same entry filters before authority can reach dispatch;
6. no proposal means no dispatch.

This run provides operational evidence for the abstention path, not statistical evidence that the strategy parameters are well calibrated.

## Actionable intelligence

### Proven

- Real Alpaca BTC market connectivity works.
- Historical warm-start completes.
- A wholly live interval becomes actionable.
- Journal reopen/schema compatibility works on the real path.
- Broker read-side evidence remains available through the experiment.
- The application can abstain cleanly without consuming its submission budget.
- No evidence indicates an accidental POST or phantom position.

### Highest-value instrumentation gap

Persist the feature vector and explicit abstention reasons for every evaluated fresh interval, even when no `BtcProposal` is created.

Minimum useful evidence:

```text
feature_status
close
baseline
trend_distance
fast_return
volatility
round_trip_cost
safety_margin
cost_hurdle
trend_positive
momentum_positive
volatility_within_limit
cost_hurdle_passed
abstention_reasons
```

The evidence should distinguish at least:

```text
FEATURES_UNAVAILABLE
STALE_FEATURES
NONPOSITIVE_TREND
NONPOSITIVE_MOMENTUM
VOLATILITY_LIMIT
COST_HURDLE
SIGNAL
```

Do not alter strategy thresholds while adding this instrumentation.

### Next experimental sequence

1. Add abstention feature/reason evidence with no authority changes.
2. Run repeated bounded hourly observations using the frozen configuration.
3. Measure empirical pass/fail frequency for each filter.
4. Only then evaluate whether the engineering thresholds are overly restrictive.
5. Separately obtain the first naturally authorized PAPER entry to exercise POST/fill/reconciliation.
6. Do not infer profitability from a successful PAPER execution.

## Remaining limitations

- Provider fee linkage/finality is still unproven; `proof_ready=false` is expected.
- No entry was submitted.
- No fill was observed.
- No automated exit path was exercised.
- Continuous BTC trading is not implemented.
- This run does not validate expected return, profitability, Sharpe ratio, drawdown, hit rate, or parameter optimality.
- Raw market evidence was hashed and retained by the operator but is not committed to Git.

## Bottom line

The 2026-09-24 canary is a successful **real-provider abstention-path integration result**.

It is strong evidence that SHAD0W can ingest real BTC history/live data, establish the fresh-live boundary, preserve execution authority, evaluate the strategy, observe the broker, and terminate without trading when no proposal is produced.

It is **not yet evidence explaining why the strategy abstained**. Feature-level no-signal instrumentation is the next required step before changing thresholds or drawing conclusions about signal scarcity.
