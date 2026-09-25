# BTC trend engineering candidate

The [bounded session contract](BTC_PAPER_SESSION.md) now describes repeated
live evaluations, persisted abstention filters and a guarded single linked exit.
The strategy configuration below is unchanged. Historical unimplemented-application
notes at the end of this document are superseded by that contract and STATUS.

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

## Typed execution and reconciliation foundations

`BtcSubmitRequest` is an explicit BTC spot subtype; the equity SubmitRequest
contract is unchanged. BTC supports positive fractional Decimal BTC quantity,
PAPER MARKET/GTC, BUY or a risk-authorized linked SELL, without extended hours.
The adapter rereads BTC asset evidence before POST: active/tradable/fractionable,
minimum order size, minimum trade increment, and price increment must be present.
Size must satisfy both the absolute increment grid and the minimum-offset grid,
and the provider's documented nine-place precision. Disagreement rejects; there
is no upward rounding. Price increment is recorded, not used as a MARKET limit.
These checks do not substitute for risk or commit-before-send authority.

Official references checked during implementation:
[crypto trading](https://docs.alpaca.markets/us/docs/crypto-trading),
[quantity constraints](https://docs.alpaca.markets/us/docs/crypto-trading-1), and
[crypto orders](https://docs.alpaca.markets/us/docs/crypto-orders).
No documentation sample minimum or increment is installed as trading authority.
Tests use synthetic asset observations and fake transport only.

The existing reducer distinguishes typed BTC attempts from equity attempts.
The legacy gross-only fixture path requires BTC cumulative fills to equal
broker exposure exactly; executable BTC authority instead requires verified
net activity effects (see the execution seam contract). Arithmetic uses
exact rational quantities to avoid caller Decimal-context rounding. Fractional
equity residue still HALTs. Empty complete initial inventory is FLAT; outstanding
entry/exit is ENTRY_PENDING/EXIT_PENDING; linked fractional exposure is HOLDING;
a complete linked exit and zero exposure is FLAT. Incomplete history or an
uncertain attempt without broker order evidence is UNRESOLVED. Missing lookup
and empty history never erase an attempt. Unlinked/conflicting activity HALTs.
BTCUSD is normalized to BTC/USD only with explicit provider crypto asset class.
Existing request and asset serialization is unchanged; new subtypes have distinct
codec tags.

## Independent BTC risk foundation

`evaluate_btc_risk` recomputes reconciliation and strategy features from supplied
immutable evidence. Entry needs FLAT, BUY, BTC/USD, current tradable asset and
legal quantity, fresh usable quote/strategy/broker/control evidence, configured
cost and volatility filters, explicit trading enablement and a clear kill switch.
Quantity and notional ceilings are explicit caller-supplied `BtcRiskPolicy`
parameters, with no trading-enabling policy defaults. Every policy field binds
its identity. Risk uses a current `BtcCashAccount`: available cash is the lesser
of cash and non-marginable buying power, never equity margin buying power.
The entry cash estimate is ask*quantity*(1+taker_fee+slippage_allowance)+cash_buffer.
The ask already includes the observed quote spread. This is an eligibility
estimate, not a guaranteed MARKET price ceiling. Both account eligibility and
explicit crypto eligibility must be established.

Exit needs HOLDING, SELL, a legal quantity no greater than linked broker exposure,
and complete unique execution evidence whose per-order sums match cumulative
fills. The current entry time is reconstructed from the first execution after
flat, not the submission time. Every completed close since that time is required
for high-water reconstruction. Missing fills, replacement chronology, conflicting
same-time sides or incomplete close history reject. An independent risk-halt
input may request an exit, but the kill switch and disabled trading freeze all
automation, including exits. ENTRY_PENDING, EXIT_PENDING, UNRESOLVED and HALTED
cannot authorize a proposal. In the legacy gross-only path, a previously uncertain attempt freezes evaluation
even if later order evidence would project HOLDING. The strict net-accounting
recovery path below additionally requires verified activity evidence and durable
halt/resume handling.

Evaluations are typed serializable evidence, not dispatch capabilities. Tests
round-trip canonical close/fill evidence through a file and reproduce exit
calculations. This proves deterministic reconstruction from supplied evidence,
not application restart acceptance or correct provider fill ingestion.

## Durable execution seam and remaining composition

The [execution seam contract](BTC_EXECUTION_SEAM.md) records the current provider
research, accounting equations, schema v5 migration, durable BTC authority,
submission ceilings, guarded dispatcher and fault-test coverage. New BTC attempt
records always require strict verified net activity accounting. The legacy gross
fixture path remains for existing reducer tests; it grants no executable BTC
authority. Strategy cost estimates never become provider fee evidence.

The dispatcher reuses this pure evaluator with strict activity evidence and
net-inventory entry chronology. Recovery of an originally uncertain submission
can support a later distinct proposal only through complete linked broker
activity, a usable persisted reconciliation, explicit resume after halt and fresh
risk. A spent original attempt can never be dispatched again.

There is still no `shadow-crypto-paper` CLI or continuous application loop.
Alpaca's documented legacy activity rows do not establish fee linkage/asset
finality or guaranteed retention. The collector therefore reports these gaps;
real activation remains blocked pending sufficient provider evidence. Verified
multi-day C1 warm-start, continuous durable history, operator controls/run lifecycle
and application composition remain to be implemented. No real PAPER order was
sent. SPY behavior and sealed P1B identities are unchanged.

Current verification receipts are recorded in the execution seam contract;
component tests do not establish real-provider or end-to-end canary acceptance.
