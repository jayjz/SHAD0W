# P5A one-shot Alpaca PAPER canary

This is a supervised, one-order PAPER procedure. It is not a daemon, a strategy
validation, an exit procedure, or permission to use an Alpaca live account.

## Preconditions

- The account is a dedicated Alpaca PAPER account with no manual activity.
- `ALPACA_PAPER_API_KEY_ID` and `ALPACA_PAPER_API_SECRET_KEY` are set outside Git.
  Do not use `ALPACA_DATA_*` credentials and never print either value.
- The operator has an existing, current, canonical SHAD0W `RiskDecision` file from
  the independent risk boundary and its stable source-opportunity identifier.
- The operator has reviewed open orders and positions and has an explicit recovery
  owner for any exposure left by a market/DAY order.

The first canary applies one explicit conservative risk-decision freshness bound:
the supplied authorized decision must be no more than 30 seconds old at the final
submission revalidation and must not be timestamped in the future. This is a
bounded one-shot assumption, not a generalized control plane.

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
closed, an order/position exists, or the account binding differs.

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
