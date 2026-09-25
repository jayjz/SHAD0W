# Bounded BTC PAPER session

`shadow-btc-paper-session` is a separate application from the unchanged one-shot
experiment. It permits one BUY and one linked SELL, never a second entry. The
journal binds mode, quantity, absolute deadline, strategy, policy and revision.
Restart cannot extend the deadline or reset either attempt. A completed or
uncertain lifecycle restarts in reconciliation-only mode. An accepted, verified
HOLDING lifecycle can continue toward its single exit within the original deadline.

The strategy retains the 72h/6h/24h configuration and 0.009 engineering hurdle.
Every newly closed wholly captured hourly interval records features, individual
entry filters and abstention reasons. NO_SIGNAL does not end the session. Quotes
never close intervals. The current partially captured hour cannot trigger a
strategy entry. A relay selection has no direct-provider fallback.

The separate `--probe` mode proposes broker plumbing BUY/SELL intents, explicitly
marked `paper_plumbing_probe`. Its price witness is not a historical strategy
feature. Independent risk still checks binding, PAPER type, controls, quote age,
asset constraints, cash, notional and lifecycle. Only strategy signal conditions
are inapplicable. Probe and strategy records must use distinct journals/scopes.

Both modes reuse the BTC dispatcher and append-only attempt journal, deterministic
client IDs, fresh risk revalidation and one POST per committed attempt. A $100
ceiling applies to the ask-price entry estimate; MARKET execution cannot promise
an absolute realized price ceiling. An existing kill-switch file disables all
submissions, including SELL. Each mode requires explicit duration and PAPER origin.

Order/position/activity polling occurs at startup, after submission, as subsequent
market events arrive, and at session end. Pending orders prohibit another proposal.
The final reconciliation remains available even if the session deadline expired.
There is no automatic retry, cancellation, forced strategy exit at timeout, or
automatic resumption of a durable uncertainty halt.

## Delayed fees and exact exit size

HOLDING requires broker orders, unique executions, established net fee effects,
and exactly matching positions. SELL additionally requires a fresh explicit
`qty_available` equal to reconciled net exposure and a legal current asset grid.
Missing availability, off-grid residue or disagreement blocks the exit; no upward
rounding or gross-fill substitution occurs.

Alpaca's [crypto fee documentation](https://docs.alpaca.markets/us/docs/crypto-fees)
still describes end-of-day posting; its
[activities reference](https://docs.alpaca.markets/us/reference/getaccountactivities-2)
describes fees commonly created the following UTC day. Neither provides the
fee finality/linkage proof required by the existing strict reducer. This session
does not invent that evidence. A real initial experimental BUY may therefore
leave an explicitly unresolved BTC position and block the linked SELL. Fake-broker
round trips with explicit coverage are implementation evidence, not real-provider
lifecycle acceptance. Continuous PAPER trading is not complete.

## Operator commands

Supply PAPER credentials through `ALPACA_PAPER_API_KEY_ID` and
`ALPACA_PAPER_API_SECRET_KEY`; market credentials through `ALPACA_DATA_KEY` and
`ALPACA_DATA_SECRET`. Never place secrets in command arguments or committed files.
Read the current BTC asset constraints and choose a legal quantity whose fresh
ask notional is at most $100. `<ACCOUNT_BINDING>` is the PAPER account number,
passed privately by the operator; summaries omit it.

Start the relay if no owner already runs it:

```sh
uv run shadow-crypto-feed-relay
```

Run a 60-second explicit plumbing probe:

```sh
uv run shadow-btc-paper-session --probe \
  --account-id '<ACCOUNT_BINDING>' --operational-scope '<UNIQUE_PROBE_SCOPE>' \
  --quantity '<CURRENT_LEGAL_BTC_QUANTITY>' --maximum-entry-notional 100 --cash-buffer 10 \
  --journal-path '<ARTIFACT_DIR>/probe.sqlite' \
  --market-evidence-path '<ARTIFACT_DIR>/probe-market.jsonl' \
  --experiment-evidence-path '<ARTIFACT_DIR>/probe-summary.json' \
  --ownership-directory '<OWNERSHIP_DIR>' --duration-seconds 60 \
  --run-id '<UNIQUE_PROBE_RUN>' --source-market-id '<UNIQUE_PROBE_SOURCE>' \
  --code-revision '<COMMIT_SHA>' --trading-enabled \
  --kill-switch-path '<ARTIFACT_DIR>/STOP' \
  --paper-endpoint https://paper-api.alpaca.markets \
  --paper-acknowledgement I-UNDERSTAND-ONE-BTC-PAPER-ENTRY-AND-EXIT \
  --market-data-relay ws://127.0.0.1:8766
```

For a strategy session, use `--session` instead of `--probe`, distinct artifact
paths/run/source/scope, and `--duration-seconds 14400` (maximum 86400). Start only
against a reconciled flat account. To reconcile an existing session, repeat its
exact command and identities; the original durable deadline still applies.
Never create a new journal to bypass an unresolved position or spent attempt.

The summary reports trade/quote counts, interval decisions, risk rejections,
committed attempts, broker IDs/statuses, fills, observed and reconciled exposure,
unresolved state and artifact paths. Raw account-bound evidence stays in the
operator's local SQLite journal. A quote-only run does not prove live trade delivery.
