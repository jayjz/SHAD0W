# BTC PAPER learning loop implementation plan

Status: ACTIVE PLAN  
Plan branch: `plan/btc-paper-learning-loop`  
Parent: `feat/shared-alpaca-crypto-relay` @ `f63da9c6ecb77de01783f69f3cf035e05b8f0147`  
Target: Alpaca PAPER only. Live-capital support remains prohibited.

## Objective

Turn the current bounded one-entry BTC PAPER experiment into a durable, bounded learning loop that can continuously observe real BTC market data, evaluate every fresh completed interval, enter and exit one position at a time, reconcile broker state, recover across restart, and emit enough structured evidence to explain every action or abstention.

The optimization target is learning velocity under hard PAPER safety constraints. Strategy parameters remain frozen while instrumentation and lifecycle execution are added.

## Provider constraints

Current Alpaca documentation establishes these supported primitives:

- Crypto orders use the standard orders API and support fractional `qty`, `market` orders, and `gtc`/ `ioc` time in force.
- PAPER order/account updates are available from `wss://paper-api.alpaca.markets/stream` through `trade_updates`.
- `trade_updates` includes fills, partial fills, cancellations, rejections and the provider order object including `client_order_id`.
- The shared local crypto market-data relay at `127.0.0.1:8766` remains market-data-only and must never gain broker authority.

## Existing foundation

Already implemented and retained:

- deterministic BTC hourly trade-built intervals;
- 72h / 6h / 24h engineering feature configuration;
- real historical BTC warm start;
- wholly fresh live trigger boundary;
- independent BTC risk evaluation;
- durable v5 journal and immutable BTC events;
- deterministic client order identity;
- journal-before-POST guarded dispatch;
- no automatic POST retry after uncertainty;
- account/asset/order/position/activity reads;
- bounded one-entry PAPER application;
- shared localhost Alpaca crypto market-data relay;
- relay mode with no silent direct-WebSocket fallback;
- real-provider NO_SIGNAL canary evidence.

## Non-negotiable invariants

1. PAPER endpoint only.
2. No provider endpoint may be supplied by arbitrary CLI/environment input for trading.
3. Maximum one active BTC position.
4. Maximum one unresolved/open order lifecycle at a time.
5. Journal authority is committed before POST.
6. A logical attempt is permanently spent after commit.
7. Unknown/timeout/disconnect POST result freezes further submissions.
8. Broker reconciliation is operational truth.
9. Kill switch freezes new dispatch.
10. Maximum entry notional remains $100 for this learning phase.
11. Maximum 20 total submissions per immutable session.
12. Strategy/risk thresholds must not be changed as part of this implementation.
13. Every completed fresh interval produces durable decision evidence, including NO_SIGNAL.
14. Restart must reconstruct state from durable evidence plus fresh broker reads.
15. Relay use never grants broker/order/risk authority.

## Why seven pushes instead of eighteen

The checklist contains dependent behaviors. Splitting every line into a separate code commit would create intermediate states that cannot be meaningfully validated, such as subscribing to order updates without a typed update contract, or enabling exits without lifecycle reconciliation.

Each push below is issue-sized, independently testable, and retires explicit checklist items. Do not start the next slice until the current slice is green and pushed.

## Push 1 — Decision telemetry

Goal: make every fresh interval scientifically useful before increasing trading frequency.

Checkboxes:

- [ ] evaluates every completed fresh interval
- [ ] persists features even for NO_SIGNAL
- [ ] persists explicit rejection reasons
- [ ] outputs session evidence summary — initial decision-summary shape

Required behavior:

- Introduce a typed immutable decision-observation value.
- Persist feature availability status, close, baseline, trend distance, six-hour return, volatility, cost hurdle, individual filter booleans and deterministic abstention reasons.
- Distinguish at minimum:
  - `FEATURES_UNAVAILABLE`
  - `STALE_FEATURES`
  - `NONPOSITIVE_TREND`
  - `NONPOSITIVE_MOMENTUM`
  - `VOLATILITY_LIMIT`
  - `COST_HURDLE`
  - `SIGNAL`
- A NO_SIGNAL decision must no longer terminate a continuous session.
- Do not alter `engineering_canary()`, proposal thresholds, risk logic or dispatch authority.

Acceptance:

- identical evidence replays to identical decision observations;
- current canary NO_SIGNAL path remains no-dispatch;
- tests prove each abstention reason;
- no new broker POST path.

## Push 2 — Durable session authority and bounded continuous loop

Goal: replace the one-shot application lifecycle with a bounded session while retaining one-at-a-time authority.

Checkboxes:

- [ ] runs continuously against Alpaca PAPER
- [ ] consumes existing BTC market stream
- [ ] max 20 submissions/session
- [ ] $100 max entry notional
- [ ] kill switch freezes dispatch
- [ ] PAPER endpoint impossible to accidentally substitute with live

Required behavior:

- Add a distinct continuous-session CLI/mode rather than weakening the one-shot canary contract.
- Prefer relay mode for live market data; retain historical REST warm start.
- Session duration must be explicitly bounded/configured.
- Immutable run config binds:
  - session ID;
  - code revision;
  - source market identity;
  - strategy config;
  - risk policy;
  - maximum submissions = 20;
  - per-period ceiling;
  - maximum entry notional = $100.
- Remove the application-level `maximum_per_run == 1` restriction only for the new session type.
- Preserve dispatcher durable ceilings.
- Trading adapter construction must hard-code/validate Alpaca PAPER trading base URL.
- Add a negative test proving live `api.alpaca.markets` cannot be selected through CLI, environment or adapter injection on the production path.
- A session continues across NO_SIGNAL intervals and stops on duration, operator stop, spent capacity, or safety halt.

Acceptance:

- fake-broker session can process multiple NO_SIGNAL intervals without exit;
- 21st attempt is impossible after 20 committed attempts;
- kill switch prevents commit/POST;
- unknown market relay URL fails closed;
- live trading URL fails closed.

## Push 3 — PAPER trade_updates ingestion

Goal: obtain real-time provider order lifecycle observations without granting the stream execution authority.

Checkboxes:

- [ ] subscribes to Alpaca `trade_updates`
- [ ] observes partial/final fills

Required behavior:

- Add a PAPER-only Alpaca trading-stream adapter for `wss://paper-api.alpaca.markets/stream`.
- Authenticate with PAPER execution credentials.
- Subscribe only to `trade_updates`.
- Decode binary/text frames according to provider behavior.
- Normalize provider events into typed provider-neutral order-update evidence.
- Preserve:
  - event type;
  - provider order ID;
  - client order ID;
  - status;
  - side;
  - quantity;
  - filled quantity;
  - fill price where present;
  - timestamp/availability.
- The stream has no submission or risk authority.
- Unexpected account/order identities fail closed or are retained as unsupported evidence according to existing reconciliation policy.
- Reconnect must never cause a POST retry.

Acceptance:

- fixtures for new, partial_fill, fill, canceled, rejected;
- duplicate update idempotence;
- conflicting duplicate rejection;
- disconnect/reconnect test;
- PAPER endpoint constant test.

## Push 4 — Reconcile after every submission and stream transition

Goal: make broker-authoritative lifecycle state the gate between submissions.

Checkboxes:

- [ ] reconciles broker state after submission
- [ ] uncertain POST freezes further dispatch
- [ ] max one active position

Required behavior:

- Compose existing REST reconciliation reads with stream updates.
- After every committed submission:
  - persist submission result;
  - ingest available trade updates;
  - reread authoritative order/position/activity state;
  - append reconciliation.
- No further strategy action may dispatch while state is:
  - ENTRY_PENDING;
  - EXIT_PENDING;
  - UNRESOLVED;
  - HALTED.
- At most one non-flat BTC exposure may exist.
- Provider stream evidence is acceleration/observation, not a substitute for authoritative REST reconciliation.
- Timeout/disconnect/malformed POST remains durable uncertainty and freezes the session until a separately valid recovery action.

Acceptance:

- partial fill blocks second entry;
- full entry fill projects HOLDING only with required evidence;
- uncertainty blocks all subsequent dispatch;
- restart with unresolved attempt remains blocked.

## Push 5 — Lifecycle-backed EXIT

Goal: complete the first actual round-trip lifecycle.

Checkboxes:

- [ ] supports ENTER when FLAT
- [ ] supports existing EXIT strategy when HOLDING
- [ ] returns to FLAT before another entry

Required behavior:

- Determine proposal mode from broker-authoritative lifecycle state, never a hard-coded `holding=False`.
- FLAT may evaluate/authorize ENTER only.
- HOLDING may evaluate/authorize EXIT only.
- EXIT quantity must be derived from linked reconciled exposure and remain legal under provider asset constraints.
- Existing high-water/exit feature logic and independent risk evaluation remain authoritative.
- ENTRY_PENDING/EXIT_PENDING/UNRESOLVED/HALTED never produce executable strategy authority.
- A new ENTER opportunity is admissible only after authoritative FLAT reconciliation following the prior EXIT.

Acceptance:

- fake lifecycle:
  `FLAT -> ENTER -> ENTRY_PENDING -> HOLDING -> EXIT -> EXIT_PENDING -> FLAT`;
- no pyramiding;
- no exit above reconciled exposure;
- missing entry/fill history blocks exit;
- second entry before FLAT is impossible.

## Push 6 — Durable restart recovery

Goal: allow a bounded session to resume safely after process termination without creating duplicate authority.

Checkboxes:

- [ ] durable restart recovery

Required behavior:

- Reopen journal, replay BTC authority, session decision observations and submission counts.
- Acquire account ownership before recovery.
- Perform fresh broker reconciliation before consuming new actionable market intervals.
- Reconstruct HOLDING/FLAT/pending/unresolved state from durable journal + broker evidence.
- Never redispatch an existing intent/client order ID.
- Preserve the 20-submission budget across restart.
- Explicit operator action remains required to resume from a durable uncertainty/halt when existing contracts require it.

Acceptance:

- crash before POST;
- crash after POST before response persistence;
- crash after partial fill;
- crash while HOLDING;
- crash after exit fill;
- all cases recover without duplicate POST or reset counters.

## Push 7 — Operational evidence and supervised PAPER soak

Goal: turn the loop into an evidence-producing experiment operator can run for hours/days.

Checkboxes:

- [ ] outputs session evidence summary
- [ ] all checklist requirements integrated together

Required behavior:

- Add bounded session summary artifact containing:
  - run identity and code revision;
  - market source/relay identity;
  - start/end times and stop reason;
  - completed fresh intervals evaluated;
  - SIGNAL / NO_SIGNAL counts;
  - abstention reason counts;
  - entry/exit proposal counts;
  - risk reject counts/reasons;
  - committed attempt count;
  - broker submission statuses;
  - partial/final fill counts;
  - reconciled lifecycle transitions;
  - maximum observed exposure;
  - unresolved/halt state;
  - proof limitations;
  - artifact hashes.
- Never store credentials.
- Add/update operator runbook.
- Run a real relay + PAPER supervised soak only after all fake/integration gates pass.

Acceptance:

- deterministic summary from persisted evidence;
- restart does not double-count;
- real provider smoke proves relay + PAPER stream + read paths before arming;
- bounded PAPER session uses at most $100 entry notional, one position, 20 total submissions.

## Validation gate before every push

Always inspect first:

```bash
git status --short --branch
git diff --stat
git diff --check
```

Focused tests for the changed surface, then full repository gate:

```bash
UV_CACHE_DIR=/tmp/shadow-uv-cache uv run pytest
UV_CACHE_DIR=/tmp/shadow-uv-cache uv run ruff check .
UV_CACHE_DIR=/tmp/shadow-uv-cache uv run ruff format --check .
UV_CACHE_DIR=/tmp/shadow-uv-cache uv run mypy src tests
git diff --check
```

Before push:

```bash
git status --short
git diff --stat
git diff --check
```

Do not stage `.codex/`.

After push:

```bash
git rev-parse HEAD
git ls-remote origin refs/heads/<branch>
git status --short --branch
```

Confirm GitHub CI before beginning the next slice.

## Branch strategy

Implementation should begin from `feat/shared-alpaca-crypto-relay` after its real two-consumer smoke test is complete.

Recommended implementation branch:

`feat/btc-paper-learning-loop`

Do not target `main` directly.

The relay branch and `integ/p5a-reconcile-canary` currently diverge by one commit each from common ancestor `9935cfa...`:

- relay branch adds the shared crypto relay;
- integration branch adds the dated canary evidence document.

Do not rebase, squash or force-push either existing branch merely to remove this harmless docs-only divergence. Bring the evidence document forward explicitly when preparing the eventual integration PR.

## Stop conditions

Stop the implementation/session immediately if any of these are observed:

- live-capital endpoint or credentials become reachable;
- account binding mismatch;
- >1 active BTC position;
- unknown submission outcome;
- conflicting order/fill evidence;
- journal replay mismatch;
- submission count exceeds configured ceiling;
- broker evidence becomes stale/incomplete at an authority boundary;
- relay silently falls back to direct provider market data;
- strategy/risk parameters drift from frozen configuration.

## Definition of done

The phase is complete when one bounded PAPER session can repeatedly:

```text
fresh interval
  -> decision evidence
  -> NO_SIGNAL and continue
     OR
  -> ENTER proposal while FLAT
  -> independent risk authorization
  -> journal commit
  -> one PAPER POST
  -> trade_updates + broker reconciliation
  -> HOLDING
  -> later EXIT proposal
  -> independent risk authorization
  -> journal commit
  -> one PAPER POST
  -> trade_updates + broker reconciliation
  -> FLAT
  -> continue until bounded stop
```

while retaining deterministic evidence and never exceeding one position, $100 entry notional, or 20 submissions.
