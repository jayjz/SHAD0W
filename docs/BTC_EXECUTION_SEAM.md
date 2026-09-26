# BTC execution seam audit and provider evidence

Current addition: the [bounded session](BTC_PAPER_SESSION.md) composes the
existing dispatcher into one entry/exit lifecycle with a distinct plumbing probe.
Strict fee evidence is unchanged. The historical composition gaps below describe
the original audit, not current session availability.

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
fail closed. Expanded provider date bounds accommodate date-only fees. The
collector removes ordinary BTC FILL spillover outside the exact requested time
window and ordinary CFEE/FEE spillover outside the inclusive requested provider
date window. Rows carrying `previous_id` or `correction_of` bypass this filtering
and retain strict correction handling. Malformed dates reject; translation still
rejects out-of-window fills or fees reaching it. Filtering does not establish fee
linkage, asset identity, historical coverage, or finality.

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

## Durable authority

Schema v5 adds one append-only `btc_events` table and immutable triggers to the
same SQLite database. `ExecutionJournal.migrate_v4` explicitly validates v4,
adds the table, and changes version atomically under account ownership. Existing
rows, UUID and timestamps survive. Older versions reject; no delete/recreate
migration exists. Old readers reject the new schema.

Canonical BTC events bind a fixed run, source-market identity, strategy config,
risk policy, code revision and positive ceilings. Attempt evidence contains the
exact evaluation/proposal/request, causal intervals and quote, controls, account,
asset, snapshot, activities, deadline and reconciliation revision. The event is
the dispatch-start marker. Results and subsequent reconciliation cuts append;
replay recomputes operational state and spent counts. Halts are sticky until an
explicit resume against usable authoritative reconciliation. A broker recovery
never erases or makes an attempt reusable.

Risk policy Decimal identity is now representation-independent, matching strategy
identity and canonical journal decoding (100 and 1E+2 cannot diverge after reopen).
This changes no risk arithmetic or canary parameters. Prior v4 had no durable BTC
policy authority to reinterpret.

Run ceiling counts all committed attempts for the configured run, including
uncertain or locally stopped sends. A journal currently permits one immutable
run binding; restart or run renaming cannot reset it. Period ceiling counts the
same records in half-open UTC intervals aligned to Unix epoch, with the explicit
configured positive `period_seconds` (86400 means UTC calendar days). No equity
session clock applies. Future run rollover/config migration remains explicit work.

## Guarded BTC submission and recovery

`BtcDispatcher` requires fresh read-only recovery on each process construction.
It accepts an authorized BTC evaluation, checks its age and exact immutable run
binding, rereads account/asset/activity/order/position evidence, and recomputes
risk against current market intervals, quote and controls. Strict reconciliation
must agree with the committed lifecycle, including exposure and linked orders.

The caller supplies the expected journal revision and a deadline within every
input validity window. The dispatcher commits exact attempt evidence under that
revision, consuming run/period capacity before invoking `submit`. The adapter
calls the final local guard after its final asset GET and immediately before the
single POST. That guard rechecks owner/thread, journal revision, controls, UTC
and monotonic deadlines. There are no automatic POST retries. This is **at most
one POST invocation per committed logical BTC intent**, not exactly-once network
delivery or guaranteed broker execution. A final local refusal also spends the
attempt. There remains an unavoidable remote race after the final local check.

Timeout, disconnect or malformed response becomes durable uncertainty. A process
crash before POST or before response persistence leaves the original committed
attempt with no result. Startup and redelivery only collect reconciliation;
404/empty history cannot free it. Recovered original broker evidence may establish
pending/holding/flat but cannot unspend its identity. Uncertainty halts persist;
resume requires a later usable reconciliation and an explicit caller action.
A distinct source opportunity then still requires fresh risk, evidence and budget.
Net fee accounting also supplies lifecycle entry time to the existing exit risk
calculation; gross fill prices/quantities remain separate evidence.

The adapter supports a requested history cut for paired activity/order reads;
positions and activities remain sequential, not atomic. Disagreement freezes
submission. No strategy, 72h/6h/24h canary or SPY MARKET/DAY sizing rule changed.

The current [Trading API endpoint reference](https://docs.alpaca.markets/us/reference/getaccountactivities-2)
also documents `order_id` filtering (useful for order fills), creation-time query
bounds, and fees commonly created on the next UTC day. It does not establish a
fee-to-execution mapping or a fee-finality/retention guarantee. The collector does
not use a filter that could hide unlinked account activity, and does not infer
settlement completeness from creation-time pagination. The legacy `CFEE` pair
symbol/description cannot safely stand in for an explicit fee asset and linkage.

## Application composition still required

There is no `shadow-crypto-paper` CLI or continuous application loop. The next
session must compose verified multi-day C1 warm-start and durable market history,
control/operator resume handling, current market callbacks, run lifecycle and
operational reporting. The component journal stores all intervals/quote/broker
inputs used by each attempt, not a continuous market-data archive. A documented
provider evidence source establishing fee asset/linkage/finality and historical
coverage is still required before Alpaca activation. Current Alpaca activity
reads deliberately cannot make the guarded dispatcher ready. No external PAPER
order or live-capital endpoint was used or added.

## Verification receipt (2026-09-24)

- Full `uv run pytest`: **1027 passed**. The sandbox-only attempt failed four
  existing localhost relay tests because socket binding was prohibited; the full
  run with localhost access passed. No external broker transport was used.
- Added 45 regressions: 14 accounting/collector, 5 journal, 25 dispatch and 1
  net-fee exit-risk test. Coverage includes delayed/missing fees, explicit zero
  fees, duplicate and malformed activities, exhausted/truncated pagination,
  exact net HOLDING/FLAT, commit-before-send, timeout/disconnect/malformed response,
  both crash boundaries, reopen, durable ceilings, revision/deadline/kill/ownership
  guards, legacy account halts and distinct-opportunity recovery.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: 143 files passed.
- `uv run mypy src tests`: 112 source files passed.
- `git diff --check`: passed.
- Commands used `UV_CACHE_DIR=/tmp/shadow-uv-cache` for the writable cache.
- Strategy and canary parameters are unchanged; no real PAPER order was submitted.
