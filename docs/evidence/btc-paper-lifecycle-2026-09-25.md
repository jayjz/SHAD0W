# Bounded BTC/USD PAPER lifecycle probe — 2026-09-25

## Scope and outcome

This was a new `paper_plumbing_probe` after the earlier timing blocker cleared.
It used a distinct operational scope, run ID, source ID, journal, evidence path,
and ownership directory. It did not use a strategy signal. The existing spent
HTTP-403 attempt and the zero-attempt timing-halted probe were left unchanged.

The probe submitted one PAPER BUY. Alpaca accepted it and the order filled. No
SELL was submitted: the existing reducer could not establish fee-linked net
inventory or fee finality, and current Alpaca documentation did not establish
that `qty_available` is exact sellable quantity after crypto fees. The probe
finished `UNRESOLVED` with broker-observed BTC exposure of `0.000124918 BTC`.
Its original 60-second deadline has elapsed; it must not be extended or
restarted to dispatch an order.

No code or accounting contract was changed. Fake-broker evidence remains
implementation evidence only.

## Timing and host clock

The latest bounded, real-host timing witness at this revision contained 8 paired
BTC/USD events. Provider timestamps parsed and preceded relay and consumer wall
receipts for every pair. Relay-to-consumer monotonic intervals were positive,
approximately 3.1–11.3 ms. Provider-to-relay deltas were approximately
15.7–111.5 ms; provider-to-consumer deltas approximately 27.0–115.6 ms.
`reproduced_inversion` was false. The host clock snapshot reported NTP
synchronized, offset approximately `+0.885 ms`, and jitter approximately
`18.984 ms`. These are diagnostic evidence only; causal validation remains
strict and unchanged.

## Read-only preflight

The trading origin was exactly `https://paper-api.alpaca.markets`. Account and
crypto status were `ACTIVE`; `trading_blocked` and `account_blocked` were false.
Cash and non-marginable buying power before entry were `$50,000`. BTC/USD was
active, tradable, and fractionable. Current asset constraints were:

| Constraint | Value |
| --- | ---: |
| `min_order_size` | `0.000011832 BTC` |
| `min_trade_increment` | `0.000000001 BTC` |
| `price_increment` | `0.000000001` |

The complete pre-entry positions and orders snapshot was empty. The previously
spent client ID returned `404 / order not found`; the other earlier probe had
zero attempts. The account activity query exhausted with no fills, fees, or
unsupported rows. Its history was not provider-verified, but the current
positions and all-order snapshot were complete and empty.

The fresh sizing quote was observed at `2026-09-25T16:53:11.981390Z`:

| Field | Value |
| --- | ---: |
| Bid | `$83,817.60` |
| Ask | `$83,844.952` |
| Ask size | `0.00099602 BTC` |
| Documented USD-pair floor at ask (`$10 / ask`) on the provider grid | `0.000119268 BTC` |
| Selected BUY quantity | `0.000125232 BTC` |
| Quantity margin above derived floor | `5.0005%` |
| Estimated notional at sizing ask | `$10.500071028864` |

The selected quantity was at least both the live provider minimum and the
documented USD-derived minimum, on the live increment grid, within nine decimal
places, comfortably below the `$100` entry ceiling, and covered by available
non-marginable buying power. Dispatch revalidated a fresh relay quote; its ask
witness was `$83,845.40`.

Alpaca's current [crypto trading contract](https://docs.alpaca.markets/us/docs/crypto-trading-1)
documents a USD-pair minimum of `$10 / USD asset price`, up to nine quantity
decimal places, fractional crypto orders, `market` and `gtc`. The [crypto order
examples](https://docs.alpaca.markets/us/docs/crypto-orders) show the supported
`BTC/USD`, quantity, BUY, market, GTC request form. The [asset endpoint](https://docs.alpaca.markets/us/reference/get-v2-assets-symbol_or_asset_id)
is the source for current per-asset constraints.

## New probe identity and artifacts

- Scope: `btc-paper-plumbing-de206ab00382`
- Run ID: `btc-paper-run-de206ab00382`
- Source ID: `btc-paper-source-de206ab00382`
- Code revision: `5cc46d490d47924c6fd307e4acedd94f6a1a9ef5`
- Journal: `/tmp/shadow-btc-paper-20260925-lifecycle-de206ab00382/probe.sqlite`
- Summary: `/tmp/shadow-btc-paper-20260925-lifecycle-de206ab00382/probe-summary.json`
- Market JSONL: not created; the session journal contains its quote/risk evidence.
- Kill switch: clear; the configured `STOP` path did not exist.

Journal SHA-256: `f9730bd39980bb6b5691a023e94ab448ea6065aff384ee78e9df07d044e77311`.
Summary SHA-256: `5898b1f430e260ae0ad59bb2abd2683f5d5391657eeb5a314d0c8f4c2730e7f9`.
The SQLite journal is retained locally and contains account-bound evidence; it is
not included in this sanitized repository note.

## Real Alpaca PAPER BUY and reconciliation

- Request class: `BTC/USD`, BUY, market, GTC, quantity `0.000125232`, PAPER.
- POST count: exactly one; one committed BUY attempt.
- Provider order ID: `db516857-70d0-41d4-8634-57d6f9c4b012`.
- Provider status: `filled` (accepted response followed by broker-authoritative
  order lookup, not treated as a fill by itself).
- FILL activity ID: `20260925125327144::3604a19b-3dcd-477c-817e-1ff56fda7b02`.
- Fill: `0.000125232 BTC` at `$83,832.57`.
- Position `qty`: `0.000124918 BTC`.
- Explicit provider `qty_available`: `0.000124918 BTC`, equal to position `qty`
  and different from gross fill.
- Activity query exhausted. One matching FILL was present. No `CFEE` or `FEE`
  activity was present; fee-complete execution IDs were empty. `history_verified`
  was false.
- Final complete snapshot at `2026-09-25T16:59:10.910044Z`: one BTC/USD position
  of `0.000124918 BTC`; zero open orders. A subsequent direct availability read
  returned the same quantity at `16:59:11.065683Z`.
- Cash after entry was `$49,989.50`.

Alpaca's [crypto fee documentation](https://docs.alpaca.markets/us/docs/crypto-fees)
says fees are charged in the credited asset and may be posted only at end of
day. The current [positions API reference](https://docs.alpaca.markets/us/reference/getallopenpositions)
describes current open-position information but does not specify that
`qty_available` proves fee-final sellable crypto or how it accounts for delayed
crypto fees. The observed `qty_available` is useful broker evidence and tracks
the provider's net position here, but does not meet the documented fee-linkage
and finality contract. No fee estimate, gross-fill substitution, or zero-fee
assumption was used.

## Lifecycle state and blocker

- BUY: submitted once, filled and reconciled to a provider position.
- SELL: not submitted; no authorized linked SELL quantity under the current
  fee-linkage/finality contract.
- Final journal state: `UNRESOLVED`, reason `execution history or fee
  linkage/finality is unproven`.
- Unresolved exposure: `0.000124918 BTC`; no open order.
- Genuine BUY → SELL → FLAT: **not completed**.

The remaining blocker is proof-grade, provider-documented fee-final sellable BTC.
The BUY's original session deadline has elapsed, so this probe cannot dispatch a
late SELL or be extended. No second identity may be used to bypass that linkage.
