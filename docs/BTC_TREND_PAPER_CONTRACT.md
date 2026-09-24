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
BTC linked cumulative fills must equal broker exposure exactly; arithmetic uses
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
cannot authorize a proposal. Any previously uncertain attempt freezes evaluation
of new submissions even if later order evidence would project HOLDING.

Evaluations are typed serializable evidence, not dispatch capabilities. Tests
round-trip canonical close/fill evidence through a file and reproduce exit
calculations. This proves deterministic reconstruction from supplied evidence,
not application restart acceptance or correct provider fill ingestion.

## Composition status and blockers

**This is a partial foundation, not the requested end-to-end BTC PAPER loop.**
There is no `shadow-crypto-paper` CLI, BTC dispatcher, or runnable BTC command.
No external PAPER order was sent during implementation. Existing SPY dispatch
and sealed P1B research identity are unchanged.

The remaining dependencies are concrete:

1. The adapter has cumulative order fills, but `read_updates` explicitly returns
   unavailable. It does not collect complete execution events with execution
   timestamps, corrections, and fee adjustments. The risk foundation requires
   complete linked fill evidence for lifecycle-backed exits.
2. Alpaca documents fees in the received asset and potentially delayed CFEE/FEE
   activities. Therefore raw BTC buy fills need not equal net broker BTC exposure.
   This reducer deliberately HALTs that mismatch. A verified normalized net-fill
   contract and complete activity collection are required; fee amounts must never
   be guessed from the strategy's cost assumptions. The provider's exact PAPER
   behavior has not been empirically verified in this sprint.
3. The existing v4 SQLite journal embeds equity RiskDecision/clock values per
   attempt. The codec can serialize BTC values, but BTC admission, configuration
   binding, durable close/fill history, persistent halt state, submission ceilings,
   and recovery verification are not composed into that journal.
4. A BTC dispatcher must consume only fresh independent authorization, commit
   before one POST, forbid replay of an attempted intent, then reconcile before
   continuation. Existing equity no-duplicate tests do not establish BTC behavior.
5. C1 capture is bounded to one hour; verified multi-day BTC history/warm-start
   composition is absent. Raw-trade history must supply the 73 contiguous
   completed canary intervals without substituting provider bars or filling gaps.

Consequently restart FLAT/HOLDING/pending/unresolved/halted dispatch invariants,
BTC no-duplicate POST, bounded submission counts, and a live evidence-to-order
round trip remain unverified. Reducer states and pure risk rejections are tested,
including uncertainty freeze and fractional equity regression coverage. A future
application must persist and recover the same evidence before claiming restart
acceptance. PAPER performance never proves profitability; no live-capital endpoint
or support is provided.

## Verification receipt for this implementation

- New BTC focused suites: 39 passed (8 strategy, 11 execution, 20 risk).
- Execution checkpoint including existing broker/journal/reducer suites: 236 passed.
- Risk checkpoint including existing equity risk/adversarial/dispatch suites: 200 passed.
- Final canonical pytest: 982 passed. Four existing localhost relay tests initially
  failed under socket-restricted sandboxing; the full rerun with localhost access
  passed. This was an environment restriction, not a product regression.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: 134 files passed.
- `uv run mypy src tests`: 104 source files passed.
- `git diff --check`: passed.
- `UV_CACHE_DIR=/tmp/shadow-uv-cache` was used because the default cache was read-only.
- No real PAPER or live-capital order was submitted. No empirical strategy,
  provider fee normalization, BTC application restart, or profitability acceptance
  is claimed by these automated receipts.
