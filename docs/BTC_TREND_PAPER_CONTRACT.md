# BTC trend engineering candidate

This is one unvalidated long/cash research candidate. Its engineering canary
parameters are not optimal, fitted, or empirically validated. PAPER performance
would not establish profitability. Live capital remains unsupported.

## Implemented deterministic strategy

`BtcIntervals` consumes normalized C1 BTC/USD trades in capture order. UTC integer
nanoseconds never pass through float. Intervals are [start,end); only a trade with
source time at or after end closes an interval. Availability is the receipt time
of that establishing trade. An initial partial interval is discarded; empty
intervals retain a missing close. Late/conflicting evidence fails closed rather
than revising completed history. Reconnects require a new contiguous warm-up.
These are observed-stream bars, not proof of exchange-feed completeness.

For completed hourly closes C, the slow baseline B is the arithmetic mean of
72 closes. Trend distance D = C/B - 1 is a signal-strength proxy, never an expected
return. Fast momentum M = C/C[-6h] - 1. Volatility sigma is the population standard
deviation of 24 simple hourly returns (25 closes), without annualization.

Entry proposes long only if D > 0, M > 0, sigma <= maximum volatility, and
D > round-trip cost + safety margin. While holding, exits propose on D <= 0,
a strict breach C < H*(1-k*sigma), or an explicit independent risk-halt input.
H is the maximum available completed close since entry; k is dimensionless.
Missing lifecycle history, gaps, or missing/future/stale features produce no signal.
The high-water helper reconstructs from supplied durable closes; it does not
itself persist them or establish the broker entry time. Risk remains independent.

## Engineering canary configuration

Every field below participates in the SHA-256 configuration identity; equivalent
Decimal representations share identity. Arithmetic uses fixed precision 34.

| Parameter | Value |
| --- | --- |
| Instrument | BTC/USD |
| Strategy version | shadow.btc-trend.v1 |
| Interval | 1 hour |
| Slow trend horizon | 72 hours |
| Fast momentum horizon | 6 hours |
| Volatility horizon | 24 hours |
| Maximum volatility | 0.03 per hourly return |
| Trailing volatility multiple | 3 |
| Estimated maker fee | 0.0015 per leg (recorded, unused by MARKET path) |
| Estimated taker fee | 0.0025 per leg |
| Spread allowance | 0.001 round trip |
| Slippage allowance | 0.001 round trip |
| Safety margin | 0.002 |
| Minimum history | 73 contiguous completed hourly closes |
| Maximum evidence age | 3660 seconds from interval end |

MARKET entry and exit each assume taker fees. Modeled round-trip cost is
0.0025 + 0.0025 + 0.001 + 0.001 = 0.007; entry requires D > 0.009.
These are explicit engineering assumptions, not observed fees or profit forecasts.
Existing fixed-quantity execution economics prices resolved fills and cannot
represent this pre-entry signal-distance hurdle; it remains unchanged.

## Composition status

This first checkpoint supplies pure features, strategy, and high-water
reconstruction only. It creates no authorization or broker submission and has no
BTC PAPER CLI. Durable lifecycle state and an independent BTC risk boundary are
required before an executable application can consume these proposals.
