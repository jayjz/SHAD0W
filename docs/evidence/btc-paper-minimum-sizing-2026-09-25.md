# BTC PAPER minimum sizing observation

This is a sanitized, read-only Alpaca PAPER sizing observation for the fresh
bounded plumbing probe. It is not an order submission or acceptance receipt.

Observed 2026-09-25 around 12:50 UTC through Alpaca's PAPER Trading API and
crypto market-data API:

| Evidence | Observation |
| --- | --- |
| PAPER account | `ACTIVE`, crypto `ACTIVE`, trading/account blocks false, USD cash and non-marginable buying power `$50,000` |
| `GET /v2/assets/BTC%2FUSD` | HTTP 200; active, tradable, fractionable BTC/USD |
| Asset `min_order_size` | `0.000011832 BTC` |
| Asset `min_trade_increment` | `0.000000001 BTC` |
| Asset `price_increment` | `0.000000001` |
| Latest ask | `$84,426.67/BTC` at `2026-09-25T12:50:08.260096794Z` |
| Current UTC daily-bar close | `$84,388.375/BTC` |
| Daily-close value of asset minimum | `$0.998483253` |
| Documented USD-pair floor | `$10 / price`; exact quantity at the lower of ask and daily close is `0.000118499734116… BTC`, or `0.000118500 BTC` on the provider's 1e-9 grid |
| Selected quantity | `0.000124425 BTC`, 5% above the strict daily-close floor, at most 9 decimal places and on the live asset increment grid |
| Estimated entry notional | `$10.50478841475` at the observed ask; below the `$100` ceiling |
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
