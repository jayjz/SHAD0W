# BTC execution seam audit and provider evidence

Starting audit: 5e57da1d7dab221fbbb6ddbca71e3cf56b3f5bab, 2026-09-23.
The pure evaluator returns serializable evidence, not a one-use capability. The
v4 attempt table requires equity RiskDecision and broker session clock evidence.
There are no BTC durable admission/counter/revision transitions or BTC guarded
send path. Gross order quantity is insufficient to explain fee-adjusted inventory.
The existing strategy, its 72h/6h/24h canary, and pure risk rules remain the basis.

## Official provider documentation checked

- [Crypto fees](https://docs.alpaca.markets/us/docs/crypto-fees): fees apply to the
  received asset. BTC/USD BUY receives BTC; SELL receives USD. Posting may lag
  until end of day. Strategy fee estimates cannot establish actual charges.
- [Account activities](https://docs.alpaca.markets/us/docs/account-activities):
  FILL provides activity ID, order ID, transaction time, quantity, price and side.
  CFEE is a crypto fee; FEE is USD-denominated. Nontrade date can mean occurrence
  or settlement date, not an execution timestamp. Cursor pagination uses the
  last activity ID, ascending direction, and at most 100 rows without `date`.
- [Crypto orders](https://docs.alpaca.markets/us/docs/crypto-orders): fractional
  MARKET/GTC requests are supported.

The documented Trading API fee shape does not prove a per-execution/order link,
explicit CFEE asset, fee finality watermark, or guaranteed historical retention.
Broker API Activity SSE documentation is a different API/credential surface; its
optional parent linkage is not imported into Trading API PAPER semantics.
The collector uses GET /v2/account/activities, binds the account via an account
read, paginates explicitly, and reports exhausted query separately from verified
historical coverage. No caller may upgrade those flags merely because a page is
short, a day elapsed, or a position happens to match. Sequential reads are not
atomic. Unknown activities remain explicit, malformed rows reject, identical
activity IDs deduplicate, and conflicting duplicates reject. Corrections/reversals
fail closed. Expanded date bounds accommodate date-only fees; trade observations
outside the requested exact window fail closed rather than being silently dropped.

## Normalized accounting

Each execution retains gross BTC quantity, USD execution price, provider activity
identity/order identity, execution time and availability. Each fee retains its
own identity, amount, asset (possibly unknown), provider date, availability and
linkage (possibly missing). Verified effects use exact arithmetic:

- BUY: BTC = gross BTC - BTC fees; USD = -(gross BTC * price) - USD fees.
- SELL: BTC = -gross BTC - BTC fees; USD = gross BTC * price - USD fees.

The general model permits established BTC or USD fees; the Alpaca adapter does
not guess either from an ambiguous fee row. Explicit complete fee evidence with
no fees permits a zero-fee execution. Missing coverage/linkage is UNRESOLVED;
unknown assets, unlinked activity, corrections and contradictory facts HALT.
Reconciliation compares verified net effects exactly to broker positions.

The old reducer call without activity evidence remains a compatibility path for
existing gross-fill fixtures and equity behavior. It is not executable BTC
recovery authority. Guarded BTC composition must request strict activity evidence.
The production Alpaca collector intentionally cannot produce verified fee/history
coverage from the documented legacy API alone. This remains an activation blocker.
