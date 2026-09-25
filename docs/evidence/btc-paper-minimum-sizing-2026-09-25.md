# BTC PAPER minimum sizing observation

This is a sanitized, read-only Alpaca PAPER sizing observation for the fresh
bounded plumbing probe. It is not an order submission or acceptance receipt.

Observed 2026-09-25 around 12:55 UTC through Alpaca's PAPER Trading API and
crypto market-data API:

| Evidence | Observation |
| --- | --- |
| PAPER account | `ACTIVE`, crypto `ACTIVE`, trading/account blocks false, USD cash and non-marginable buying power `$50,000` |
| `GET /v2/assets/BTC%2FUSD` | HTTP 200; active, tradable, fractionable BTC/USD |
| Asset `min_order_size` | `0.000011832 BTC` |
| Asset `min_trade_increment` | `0.000000001 BTC` |
| Asset `price_increment` | `0.000000001` |
| Latest ask | `$84,453.10/BTC` at `2026-09-25T12:55:24.059571Z`; displayed ask size `0.0009901 BTC` |
| Current UTC daily-bar close | `$84,424.713/BTC` |
| Daily-close value of asset minimum | `$0.998913204216` |
| Documented USD-pair floor | `$10 / price`; exact quantity at the lower of ask and daily close is `0.000118448729579… BTC`, or `0.000118449 BTC` on the provider's 1e-9 grid |
| Selected quantity | `0.000124372 BTC`, 5% above the strict daily-close floor, at most 9 decimal places and on the live asset increment grid |
| Estimated entry notional | `$10.5036009532` at the observed ask; below the `$100` ceiling |
| Open orders / positions | zero open orders; no positions; no BTC position or available quantity |

The live asset metadata minimum is roughly one tenth of the official documented
USD-pair minimum at the current close. Sizing therefore uses the larger
documented floor, with a 5% quantity cushion. Risk independently rechecks the
configured quantity against the fresh ask-derived floor before dispatch; a
quote decline that makes it too small is rejected without POST. No quantity is
rounded or changed by the dispatcher.

The earlier one-shot attempt remains spent in its original journal, whose last
summary is unresolved because its committed attempt has no broker order. This
probe uses distinct run, source, scope, journal, ownership and artifact paths.
The PAPER account's current broker inventory is flat and has no open orders.

## Probe outcome and timing blocker

The new bounded probe reached local application startup with trading enabled,
the PAPER acknowledgement, a clear kill-switch path, a 60-second deadline,
the fixed PAPER endpoint and the distinct identities above. It stopped with
`MarketDataValidationError` before any quote or trade was counted. The new
journal records zero committed attempts, one configure, one reconciliation and
one halt. Final read-only PAPER reads showed no positions or open orders; no
POST was submitted. The journal remains `UNRESOLVED` because provider activity
history was not proven.

Read-only inspection of four consecutive BTC quote events forwarded by the
localhost relay found each provider observation timestamp about 300 ms later
than its local receipt time (receipt minus observation: `-299.900`, `-299.804`,
`-299.750` and `-299.655` ms). The market-data contract correctly rejects
availability earlier than observation. No timestamp was adjusted. The relay
was stopped after diagnosis.

Artifacts are in `/tmp/shadow-btc-paper-20260925-minimum-probe/`:

| Artifact | SHA-256 |
| --- | --- |
| `probe.sqlite` | `932f8aad10377fc70865d78c902c1dd557e6b84e06976919579b4a317099f4a8` |
| `probe-summary.json` | `d6b4a07bcffe94fc8299864ec4eb706e5f5fb3066b40304524401e1a5c37040d` |

The market JSONL artifact was not created because validation stopped before
market capture. The original spent-attempt journal and its SHA-256 remain
unchanged.
