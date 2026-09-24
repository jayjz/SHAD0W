# Crypto market-data contract — C0

Status: C0 provider-neutral values and their local validation are implemented in
`shadow.domain.crypto_market`. C1.1/C1.2 normalization, deterministic book
reconstruction and offline capture/replay are implemented. C1.3 adds bounded
Alpaca `crypto/us` transport, data-only credentials, reset-gated capture and
clock-free replay; descriptive measurements remain planned.

## Authority and scope

C1 covers exactly `BTC/USD` and `ETH/USD`, spot pairs quoted in USD. Prices are
quote-currency units per base unit; sizes are base-asset units, not equity lots.
The domain contains no provider SDK, network, execution, risk or strategy imports.
There is no order-submission authority. Existing equity/P4A and P5A contracts,
market/DAY intents, and the one-share SPY PAPER probe remain unchanged.

Raw trade, quote and book evidence is authoritative input for this research lane.
Normalized events and later measurements must remain traceable to received input;
neither derived measurements nor provider bars may substitute for that evidence.
"Authoritative" here means research input, not proof of provider completeness,
executable liquidity, broker state, or profitability. Provider bars are not
authoritative research primitives: they can include quote midpoints and late-trade
revisions. No bar subscription or bar-derived fallback is part of C1.

## Implemented provider-neutral values

All values are frozen, slotted dataclasses. Invalid values raise the existing
`MarketDataValidationError`. No normalization, sorting, deduplication or repair
occurs at construction. Imports are explicit from `shadow.domain.crypto_market`;
existing domain re-exports are unchanged.

| Value | Contract |
| --- | --- |
| `UtcNanoseconds(value)` | Exact integer Unix-epoch nanoseconds, UTC; bool, float, Decimal, datetime and string are rejected. Supported range is calendar years 1–9999: -62135596800000000000 through 253402300799999999999 inclusive. |
| `CryptoTrade` | Instrument, positive finite Decimal price and size, nonempty trimmed opaque string trade ID, typed `TakerSide.BUY/SELL`, times, availability semantics, provenance. |
| `CryptoQuote` | Instrument, positive finite Decimal bid/ask prices, mandatory nonnegative finite Decimal bid/ask sizes, times, availability semantics, provenance. Locked/crossed observations are preserved and classified using existing `QuoteMarketState`. |
| `BookLevel` | Positive finite Decimal price, nonnegative finite Decimal absolute size. Zero means absence/deletion, not a zero-price level. |
| `CryptoBookEvent` | Instrument, typed `BookAction.RESET/UPDATE`, required immutable bid/ask tuples of `BookLevel`, times, availability semantics, provenance. Either side may be empty; repeated prices within a side reject, including numerically equal Decimal representations. |
| `BookQuality` | Closed vocabulary: `UNKNOWN`, `RESET_SNAPSHOT`, `RECONSTRUCTED_UNVERIFIED`, `UNTRUSTED`. Values do not implement transitions or grant authority. |

Each event reuses `Instrument` but restricts it to the two C1 pairs. Generic
`Instrument` remains unchanged. `Provenance` retains its truthful source/timezone/
session-description meaning; it is not an execution identity. Production C1 will
use source `alpaca:crypto:us`; synthetic sources are allowed for offline tests.
Trade IDs remain opaque; their uniqueness scope is not asserted by the domain.
Taker side describes the aggressor, never an intended order.

All numeric inputs must already be Decimal; no implicit conversion is performed.
Finite original values retain exact precision independently of the caller's
Decimal context. Signed numerical zero is allowed wherever zero is allowed.
Trade size zero is invalid; quote size zero and book size zero are valid.

## Time and identity

`observation_time` is the exact provider event instant. `availability_time` is a
distinct local receipt instant, and must be greater than or equal to observation,
even at a one-nanosecond difference. Only typed
`AvailabilitySemantics.SYSTEM_RECEIVED` is accepted in this capture lane.

UTC integers carry no local timezone. There is deliberately no datetime or
RFC3339 conversion API in C0. The future adapter must translate explicit offsets
to the same UTC epoch integer, preserving every fractional digit up to nine,
rejecting naive/invalid timestamps, and never routing nanoseconds through float or
datetime microsecond precision. Timezone-equivalence parsing tests belong to that
adapter. The domain cannot prove that a caller's integer is truthful receipt time.

Future collection must sample UTC wall-clock nanoseconds and process-relative
monotonic time immediately after frame receipt and before decoding. Frame elements
share receipt times and retain array indices. Reject events after their receipt;
stop on backwards local receipt-wall-clock movement rather than repairing it.
Replay must use persisted times, never an ambient clock.

`availability_time - observation_time` is **provider-event-time to local-receipt
delay**. It is not exchange latency or network latency. Clock accuracy, provider
processing and buffering remain unknown. Nanosecond representation is not a claim
of nanosecond accuracy.

`session_id` identifies capture/run evidence only. It confers no causal or
execution authority. Future records require contiguous local sequences from zero,
connection epochs, frame sequences and element indices. Arrival order is retained;
timestamps and hashes are not invented provider sequence numbers or event IDs.
State must be isolated by provider/location/instrument.

## Planned book semantics (no reducer in C0)

Validate an entire event before applying it atomically. RESET replaces both maps
with the supplied nonzero levels. UPDATE adds or replaces each exact price with
its supplied absolute nonzero size; zero removes that price. Delete of an absent
level and repeated identical assignment are explicit no-ops. Never add sizes.
Keep all delivered depth; sorted projections use descending bids/ascending asks.

`UNKNOWN` means no valid reset. A valid reset establishes `RESET_SNAPSHOT`.
Subsequent unbroken local updates produce `RECONSTRUCTED_UNVERIFIED`, not proof of
provider completeness. Detected discontinuity or ambiguous/malformed book evidence
produces `UNTRUSTED`; only a valid reset can restore snapshot authority. A reset
cannot regress the accepted book-time watermark within its connection epoch.
New connections start a new baseline. UNKNOWN/UNTRUSTED books cannot supply usable
book measurements. No state container or transition logic is introduced in C0.

| Input or failure | Required future disposition |
| --- | --- |
| Missing side, malformed number, negative price/size, duplicate price within one side | Reject atomically; affected book becomes UNTRUSTED. Empty sides are valid. |
| Locked/crossed/empty book | Preserve and classify; do not repair or claim executable liquidity. Dependent measurements are unavailable. |
| Identical immediate book/quote repetition | Retain delivery, flag repetition; do not create another OFI increment. |
| Older book event, or different book payloads at one timestamp | Preserve evidence, mark UNTRUSTED, await reset. Conservative ambiguity policy, not timestamp uniqueness. |
| Nonconsecutive identical book payload | Do not globally deduplicate by hash; apply chronology/ambiguity rules. |
| Same trade ID within instrument/connection epoch | Identical event is retained without a second signed-flow contribution; conflicting event terminates capture as failed. No cross-epoch uniqueness claim. |
| Older/equal-time variant quote | Retain, invalidate current quote state until a strictly later valid quote; do not overwrite book state. |
| Late valid trade | Consume at receipt, never backdate measurement availability. |
| Disconnect/reconnect, lost or oversized frame | Invalidate both books and rolling history; require per-symbol resets. |
| UPDATE before reset | Retain as awaiting reset; do not apply. Missing reset fails after the configured deadline. |
| Malformed frame or unknown affected symbol | Invalidate both books. |

Quotes and book messages remain independent evidence; neither silently repairs the
other. There is no documented atomic synchronization or provider sequence proof.

## Planned append-only evidence boundary

Reserve schema **`shadow.crypto-market.v1`** (`CRYPTO_MARKET_SCHEMA`). It is separate
from `shadow.live.v1`; C0 implements no codec, capture configuration or writer.

The header must bind schema, session ID, code revision, provider, crypto location,
instruments, requested channels, implementation versions, numeric/time policies,
capture limits and measurement configuration. Records must retain local sequence,
connection/frame/element identity, kind, normalized event, observation/availability
times, monotonic receipt time, source timestamp, input reference, deterministic
disposition, quality, derived snapshot and state/hash-chain evidence. Invalid input
and controls may lack an observation time; never invent one. Authentication data
must never enter capture evidence. Preserve exact received-data identity separately
from canonical normalized identity; the eventual codec must define that encoding
before implementation.

Exclusive creation, append-only complete records, bounded resources and terminal
status are required. Storage failure stops collection. Corrupt complete records,
sequence gaps, unknown schemas, hash/output mismatch or data after terminal fail
closed. A missing terminal or partial final line is incomplete, never a complete
research dataset. Diagnostic verified prefixes do not authorize continued capture
or research completeness claims.

Same capture bytes must reproduce the same typed sequence, reconstructed states,
descriptive states and digests offline. Include accumulated evidence lineage in
state identity: changing an event later overwritten by another event must still
change evidence identity, even if final visible levels coincide. Hashes do not
authenticate provider truth or prevent deliberate whole-artifact rewriting.

## Research boundary

H0: Alpaca BTC/USD and ETH/USD market-microstructure evidence contains no stable
short-horizon predictive information after chronological validation and realistic
costs.

H1: A small predeclared set of order-flow, spread, depth, and microprice features
contains stable out-of-sample information at selected horizons.

These are untested hypotheses. Candidate measurements include book OFI, top/depth
imbalance, spread, weighted-midpoint microprice proxy, signed trade flow, update
delivery intensity, and liquidity/adverse-selection proxies. No measurements or
models are implemented by C0. A weighted midpoint is not a fitted microprice model;
delivery intensity is not proven unique exchange-event intensity.

Later experiments may examine 100 ms, 500 ms, 1 s, 2 s, 5 s and 30 s. Delay,
sparsity, clock uncertainty and discontinuities may make any horizon unusable.
Predeclare features, chronological splits, overlap purging, costs and admissibility
rules; retain failed/negative results. No predictive or profitability claim follows
from successful reconstruction.

Research motivation from the planning review:

| Source | Setting/horizon | Motivation and limitation |
| --- | --- | --- |
| [Anantha & Jain (2024)](https://arxiv.org/html/2408.03594v1) | NSE NIFTY futures, one historical day; one-minute forecasts | Signed trade-arrival imbalance/intensity, not book OFI or proof of spot returns. |
| [Blakely (2024)](https://arxiv.org/html/2411.13594v1) | TEM/TSLA cash equities via Databento; future price-update steps | Best/deeper imbalance, spread and microprice; equity order data and event-time horizons do not establish Alpaca transfer. |
| [Microstructure alpha (2026)](https://www.frontiersin.org/journals/blockchain/articles/10.3389/fbloc.2026.1811716/full) | Binance spot/perpetuals including BTC/ETH; five-minute returns | Flow, spread and toxicity proxies motivate study; bar proxies are not direct book depth and cost/generalization limitations remain. |

## Provider references and future dependencies

The planning review used official [crypto stream documentation](https://docs.alpaca.markets/us/docs/real-time-crypto-pricing-data)
and [WebSocket protocol](https://docs.alpaca.markets/us/docs/streaming-market-data).
C1 selects `wss://stream.data.alpaca.markets/v1beta3/crypto/us`, subscribes only to
trades/quotes/orderbooks for both pairs, preserves nanosecond timestamps, and waits
for actual resets. `us-1`/`eu-1` are different locations; no automatic fallback.
`r:true` is reset; absent/false `r` is update. Reset delivery is described as typical,
not guaranteed. Provider sequencing, ID lifetime uniqueness and clock accuracy
remain unproven. Recheck provider wire details when implementing the adapter.

[Crypto trading](https://docs.alpaca.markets/us/docs/crypto-trading) documents asset
class, fractionable/tradable flags and min_order_size/min_trade_increment/
price_increment fields. Illustrative values are not current per-asset limits.
[Crypto orders](https://docs.alpaca.markets/us/docs/crypto-orders) describe market,
limit, stop_limit, GTC/IOC and qty/notional; trading is 24/7. Conflicting generic
notional prose and GTC example wording require clarification before any separately
authorized execution work. None changes SHAD0W's execution envelope.

C0 itself adds no connectivity, parsing, reducer, persistence, replay, feature
calculation, execution, risk, strategy, position lifecycle or continuous operation.
C1.1/C1.2 provide synthetic/offline normalization, deterministic book reconstruction,
session lifecycle ownership, canonical hash-chained evidence, and clock-free replay.
C1.3 adds a bounded data-only WebSocket collector that samples wall-clock and
monotonic receipt nanoseconds before decoding every frame, requires exact
authentication/subscription acknowledgement and fresh BTC/USD plus ETH/USD resets,
and fails closed on malformed data or bounded-resource exhaustion. Measurements and
all execution authority remain out of scope.

## BTC trend consumer

The separately assigned [BTC trend candidate](BTC_TREND_PAPER_CONTRACT.md) consumes
C1 trades into causal completed hourly intervals. C1 capture itself retains no
strategy, risk, or execution authority. The candidate does not alter P1B identity.

BTC now has a separately typed PAPER transport and pure cash-limited risk/reducer
foundation. This does not add order authority to C1 capture. A bounded BTC
application and multi-day raw-trade warm-start remain absent; the BTC contract
records execution-event/fee and durable lifecycle dependencies.

When multiple local processes need Alpaca crypto data, the bounded
`shadow-crypto-feed-relay` may be the sole WebSocket owner. Its fixed
localhost-only protocol always owns BTC/USD `bars` and may add fixed BTC/USD
`trades`/`quotes` only for the existing SHAD0W trade-built PAPER source. It does
not change C1 capture's BTC/USD-plus-ETH/USD trade/quote/orderbook contract,
does not synthesize data, and does not grant any execution authority.

## BTC application historical context

The bounded BTC application may retrieve raw `crypto/us` BTC/USD trades through
Alpaca's documented historical trades API, with ascending, bounded pagination.
Historical records use `alpaca:crypto:us:historical-fetch` provenance and actual
page retrieval availability; original historical receipt is unknown. They are
feature context, never fresh trigger evidence. No provider bars or interpolation
are used. Completed hour closes use the same observation-ordered last-trade rule
as C1, including explicit empty hours. Availability is the maximum receipt of
inputs and the later boundary trade. Across the historical/live handoff this can
be later than a buffered live receipt, which is retained unchanged.

A wholly live completed interval beginning at or after the explicit handoff is
required before evaluation. A mixed interval cannot trigger. Restarts establish a
new handoff and backfill from the last completed end, preserving all older closes
for high-water reconstruction. The market JSONL evidence is append-only, fsynced,
hash-linked and anchored by path, byte length and digest in application evidence
in the existing owned SQLite journal. Missing files, altered bytes, complete-line
truncation and uncommitted tails reject; no automatic truncation/repair exists.
Application records are separate from the BTC execution authority projection.
