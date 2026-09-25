# Bounded BTC session implementation and rejected PAPER probe

## Revision and scope

Implementation/probe revision: `24231a58920bf13e4386ee97a6c79b802d118b18`.
Started at `09a692e` on `feat/btc-paper-learning-loop`, equal to its remote tip.
Fetched remote refs; integration merge base was `9935cfa`. Read integration
evidence from `origin/integ/p5a-reconcile-canary` at `024139d`. No merge, history
rewrite or push occurred. Existing untracked `.codex/` work was preserved.

Follow-up changes expose committed submission status in the summary, evaluate
every newly closed interval in a batch, and add control/deadline/probe-boundary
regressions. They do not retry or alter the real attempt.

## Verification

- Focused session and BTC adapter tests: **32 passed**.
- `uv run pytest -q`: **1102 passed**, 9.98 seconds.
- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: 159 files passed.
- `uv run mypy src tests`: 123 source files passed.
- `git diff --check`: passed.
- Commands used `UV_CACHE_DIR=/tmp/shadow-uv-cache`.
- Initial dependency resolution and localhost access were blocked by the sandbox;
  approved dependency installation and network-enabled checks passed. These were
  environment failures, not strategy failures.

Fake-broker tests establish unchanged-strategy ENTER→EXIT, explicitly separate
probe BUY→SELL, fee-adjusted exit size, partial fill, rejection, lost response
after acceptance, both crash boundaries, verified-HOLDING restart, duplicate
client identity, stale quote, inventory disagreement, unavailable sellable BTC,
repeated NO_SIGNAL, trade-versus-quote interval closure, controls, and immutable
deadline. Synthetic fee/history coverage is not real Alpaca evidence.

## Real data observations

Started the existing localhost crypto relay after confirming no relay listener
was available. No direct-provider fallback was opened by the consumer.

- First 20-second read-only application smoke: 0 trades, 13 quotes.
- Next 60-second smoke: 0 trades, 41 quotes.
- Final 180-second smoke: **1 BTC/USD trade and 66 quotes** reached `_RelayLive`.

The last observation confirms actual trade delivery, with sparse trade activity
in this window. It does not establish continuous hourly feed completeness.
No real strategy signal or completed strategy lifecycle was observed in this run.

## Real PAPER plumbing probe

The account was flat and had no observed orders before dispatch. Current asset
GET established tradability, minimum quantity `0.000011832`, and increment
`0.000000001`. The minimum passed the existing exact size/increment/nine-place
checks. Configured entry ceiling was $100 and cash buffer $10.

Ran the [documented probe command](../BTC_PAPER_SESSION.md) for 60 seconds with
the exact current minimum, explicit PAPER origin, trading enablement, kill-switch
path, account ownership and immutable journal. A private driver supplied account
binding without displaying it. No credentials or raw account IDs are in this record.

- Source: `paper_plumbing_probe`; **not a strategy signal**.
- Quote ask witness: `84046.245` USD/BTC.
- One committed BUY attempt reached the existing guarded POST.
- Provider result: **HTTP 403, definitive rejection**.
- Provider order ID: none.
- Observed fills: none.
- SELL attempts: zero.
- Initial session result: `UNRESOLVED_OR_HALTED`.
- Repeated exact session command: `RESTART_RECONCILIATION_ONLY`; attempt count
  remained one. No identity reset or retry occurred.

The adapter retained HTTP status but not the rejection response body. Read-only
checks showed account status ACTIVE, crypto status ACTIVE, trading/account blocks
false, suspend_trade false, and sufficient cash. Those facts do not explain the
403. The minimum-size request's quote notional was below $1; this is a possible
size-related lead, not an established rejection cause. Passing the asset grid
does not prove broker acceptance.

## Final broker and journal state

At `2026-09-25T07:03:43.775366+00:00`, a fresh broker snapshot showed **zero
positions and zero orders**. A subsequent lookup by the committed deterministic
client ID returned `not_found`. No real PAPER BUY or SELL completed. No broker
exposure was observed.

The journal intentionally remains UNRESOLVED with reason
`committed attempt lacks broker order`; absence does not reset a spent attempt.
This is unresolved intent evidence, not a claim that BTC is held. No live-capital
endpoint was used.

Runtime originals: `/tmp/shadow-btc-paper-20260925-lgMsFZ/`.
Preserved ignored artifact copies: `artifacts/btc-session-20260925/`.
The probe does not create historical market JSONL; its quotes and exact risk/
broker inputs are persisted in the SQLite attempt evidence.

| Artifact | SHA-256 |
| --- | --- |
| `probe.sqlite` | `4adb242750b410d23d3cfad62b90b75da18687a943ea97f32e1446396218ceb2` |
| `probe-summary.json` (restart receipt) | `39ac0c7aa0d8559a008c6d8ea622d968b65b953d44b77d65c0fe2891ffa7cc6f` |

The immediate execution blocker is the unexplained HTTP 403. Separately, even
after an accepted entry, Alpaca's delayed fee linkage/finality must establish exact
net BTC before the strict automatic SELL gate can open. This session cannot claim
one remaining issue or real continuous PAPER acceptance: both limitations remain.
No threshold was loosened and no signal was forced.
