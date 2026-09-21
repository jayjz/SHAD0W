# P5A one-shot Alpaca PAPER canary

This is a supervised, one-order PAPER procedure. It is not a daemon, a strategy
validation, an exit procedure, or permission to use an Alpaca live account.

## Preconditions

- The account is a dedicated Alpaca PAPER account with no manual activity.
- `ALPACA_PAPER_API_KEY_ID` and `ALPACA_PAPER_API_SECRET_KEY` are set outside Git.
  Do not use `ALPACA_DATA_*` credentials and never print either value.
- Market-data names are `ALPACA_DATA_KEY`, `ALPACA_DATA_SECRET`, and
  `ALPACA_DATA_FEED`; PAPER names are `ALPACA_PAPER_API_KEY_ID`,
  `ALPACA_PAPER_API_SECRET_KEY`, and `SHADOW_PAPER_ACCOUNT_ID`. For an ordinary
  Alpaca Trading API account the data and PAPER keypairs may intentionally have
  identical underlying values. The separate names are deliberate authority
  boundaries, not a requirement to provision duplicate keys.
- The operator has an existing, current, canonical SHAD0W `RiskDecision` file from
  the independent risk boundary and its stable source-opportunity identifier.
- The operator has reviewed open orders and positions and has an explicit recovery
  owner for any exposure left by a market/DAY order.

The first canary applies one explicit conservative risk-decision freshness bound:
the supplied authorized decision must be no more than 30 seconds old at the final
submission revalidation and must not be timestamped in the future. This is a
bounded one-shot assumption, not a generalized control plane.

## Read-only candidate preparation

`shadow-paper-prepare` is the only supported bridge from a verified P4A capture
to a canary-compatible decision. It only performs PAPER `GET` reads. It never
opens a journal, claims a gate capability, or invokes broker submission. Supply a
completed stopped capture and the explicit record sequence containing the current
quote-ready SPY candidate. The capture must have been produced with stable,
non-session source/configuration identities; session IDs remain display/capture
identities and are not executable causal identity.

```bash
shadow-paper-prepare \
  --capture-path "$PWD/shadow-spy-observation.jsonl" --candidate-sequence <sequence> \
  --risk-decision-path "$PWD/current-risk-decision.canonical.json" \
  --evidence-path "$PWD/current-risk-preparation.json" \
  --account-id "$SHADOW_PAPER_ACCOUNT_ID" --operational-scope paper-primary \
  --symbol SPY --feed-lineage alpaca:iex:spy-1m-v1 \
  --strategy-configuration-id spy-mean-reversion-v1 \
  --quantity-configuration-id paper-spy-one-share-v1 \
  --risk-policy-id paper-spy-canary-v1 --trading-enabled
```

It writes a canonical `RiskDecision` only when the independent evaluator
authorizes. A rejection or malformed/stale/incomplete/currently unsafe input
writes no decision file. The sanitized preparation evidence records the decision
identity, disposition, rejection reasons when applicable, and the deterministic
`source_opportunity_id`; it contains no credentials. The command prints that ID
on successful authorization. Preserve it and pass exactly that value to the
armed canary. The canary independently recomputes it from the supplied decision,
account binding, and scope, so an ID from another candidate cannot be substituted.

## Read-only connectivity phase

This makes only PAPER `GET` requests. It does not create a journal or order.

```bash
shadow-paper-canary --read-only --account-id "$SHADOW_PAPER_ACCOUNT_ID" \
  --operational-scope paper-primary --symbol SPY \
  --ownership-directory "$PWD/.paper-owner" \
  --journal-path "$PWD/.paper-journal.sqlite" \
  --evidence-path "$PWD/paper-read-only.json"
```

Inspect the artifact and stop unless account, clock, SPY asset, positions, and
orders are all reported as typed broker evidence. Do not arm while the market is
closed, a position exists, any broker order is outstanding, or the account binding
differs. Historical `filled`, `canceled`, `expired`, and `rejected` rows remain
reconciliation evidence and do not alone block the one-shot entry; every other
order lifecycle state is conservatively treated as outstanding.

`SHADOW_PAPER_ACCOUNT_ID` is the stable SHAD0W binding and must contain Alpaca's
human-facing `account_number` (for example `PA34U6RNDIPQ`), not the distinct UUID
in the account response's `id` field. The read-only check requires both fields,
matches `account_number` exactly, and records the provider UUID as non-secret
evidence.

## HUMAN-RUN first order

This is the only command that can call `POST /v2/orders`. It needs an already
authorized, canonical risk decision; it accepts only the matching one-share SPY
BUY request, constructs its deterministic client ID, commits it locally, and then
performs one submit attempt.

```bash
shadow-paper-canary --arm-paper-order \
  --paper-acknowledgement I-UNDERSTAND-THIS-SUBMITS-ONE-ALPACA-PAPER-ORDER \
  --account-id "$SHADOW_PAPER_ACCOUNT_ID" --operational-scope paper-primary \
  --symbol SPY --ownership-directory "$PWD/.paper-owner" \
  --journal-path "$PWD/.paper-journal.sqlite" \
  --risk-decision-path "$PWD/current-risk-decision.canonical.json" \
  --source-opportunity-id "$SHADOW_SOURCE_OPPORTUNITY_ID" \
  --daily-submission-limit 2 \
  --evidence-path "$PWD/paper-canary-result.json"
```

On any timeout, disconnect, malformed response, response-persistence error, 404
lookup, incomplete snapshot, or conflict, treat the result as unresolved. Do not
rerun the armed command to retry the order: it will only reconcile the committed
client ID and halt if evidence remains insufficient. Preserve the journal and all
JSON evidence for operator review.
