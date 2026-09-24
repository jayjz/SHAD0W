# Codex execution prompts — BTC PAPER learning loop

Use this file as the task queue for `feat/btc-paper-learning-loop`.

These prompts intentionally follow an issue-style structure: goal, scope, relevant files, invariants, acceptance tests and delivery. They point Codex to the durable plan rather than repeating the entire repository map.

## Common execution contract

For every task:

- Read `AGENTS.md`.
- Read only the plan section and source/contracts relevant to the assigned slice.
- Preserve untracked `.codex/`.
- Inspect current branch/status before edits.
- Do not rebase, squash, amend pushed commits, force-push, merge `main`, or rewrite history.
- Do not change frozen BTC strategy thresholds unless explicitly assigned in a later research task.
- Do not add live-capital support.
- Run focused tests, then the full standard gate.
- Review the complete diff before commit.
- Make one coherent commit and push only after all applicable checks pass.
- Report observed facts separately from assumptions/unverified provider behavior.

Full gate:

```bash
UV_CACHE_DIR=/tmp/shadow-uv-cache uv run pytest
UV_CACHE_DIR=/tmp/shadow-uv-cache uv run ruff check .
UV_CACHE_DIR=/tmp/shadow-uv-cache uv run ruff format --check .
UV_CACHE_DIR=/tmp/shadow-uv-cache uv run mypy src tests
git diff --check
```

### Task 0 — establish implementation branch and relay smoke

Goal: establish a clean implementation baseline from the shared crypto relay and prove the relay works with the real SHAD0W BTC PAPER consumer before changing lifecycle behavior.

Start from:
`feat/shared-alpaca-crypto-relay` @ `f63da9c6ecb77de01783f69f3cf035e05b8f0147`.

Read:
- `docs/ALPACA_CRYPTO_FEED_RELAY.md`
- `docs/BTC_PAPER_LEARNING_LOOP_PLAN.md`
- relay implementation/tests
- current `crypto_paper.py`

Actions:
1. Verify remote SHA and CI.
2. Create/switch to `feat/btc-paper-learning-loop`.
3. Do not modify code unless the manual relay smoke exposes a real defect.
4. Run the documented local relay health check and a bounded SHAD0W market-data relay smoke.
5. Verify relay mode sends no provider auth and has no direct fallback.
6. Record a sanitized smoke receipt in docs/evidence only if real-provider smoke succeeds.

Stop if 406, relay reconnect instability, unsupported frames, duplicate upstream ownership, or direct fallback occurs.

No broker POST is required for this task.

### Task 1 — persist every decision and NO_SIGNAL reason

Implement Push 1 from `docs/BTC_PAPER_LEARNING_LOOP_PLAN.md`.

Primary files:
- `src/shadow/features/btc_trend.py`
- `src/shadow/strategies/btc_trend.py`
- `src/shadow/application/crypto_paper.py`
- journal/application evidence boundary
- focused BTC strategy/application tests

Requirements:
- typed immutable decision observation;
- feature values persisted for valid NO_SIGNAL;
- explicit reason set;
- unavailable/stale evidence distinguished from threshold rejection;
- every newly completed fresh interval emits one decision observation;
- NO_SIGNAL does not terminate the future continuous-session path;
- existing one-shot semantics may remain unchanged until Task 2;
- strategy parameters unchanged.

Add deterministic tests for every reason and replay.

Suggested commit:
`feat(evidence): persist BTC decision observations`

### Task 2 — bounded continuous PAPER session

Implement Push 2.

Primary files:
- `src/shadow/application/crypto_paper.py` or a new focused session application module if that reduces one-shot regression risk;
- `src/shadow/execution/btc_authority.py`;
- `src/shadow/execution/btc_journal.py`;
- paper broker endpoint construction;
- CLI tests.

Requirements:
- new explicit continuous-session mode;
- relay-compatible live source;
- continue after NO_SIGNAL;
- immutable ceiling of 20 submissions;
- one-position design boundary retained;
- $100 maximum entry notional;
- kill switch freezes new dispatch;
- hard PAPER trading endpoint;
- negative tests that live endpoint cannot be selected.

Do not add trade-update WebSocket yet.

Suggested commit:
`feat(application): add bounded BTC paper session`

### Task 3 — PAPER trade_updates adapter

Implement Push 3.

Before coding, verify current Alpaca documentation for:
- PAPER trading stream endpoint;
- auth/listen protocol;
- binary/text frame behavior;
- `trade_updates` event shapes relevant to crypto.

Primary files:
- new Alpaca PAPER trading-stream adapter;
- provider-neutral broker/order update model if needed;
- adapter tests/fixtures;
- provider contract docs.

Requirements:
- PAPER-only endpoint;
- subscribe only to `trade_updates`;
- normalize partial/final fills, cancel, reject;
- correlate using provider order ID and deterministic client order ID;
- no submission authority;
- bounded reconnect;
- reconnect never triggers POST.

Suggested commit:
`feat(alpaca): ingest paper trade updates`

### Task 4 — compose stream observations with reconciliation

Implement Push 4.

Primary files:
- BTC reconciliation;
- dispatcher/application composition;
- BTC journal;
- trade-update adapter;
- fake broker/integration tests.

Requirements:
- fresh reconciliation after submission and material order transitions;
- stream does not replace authoritative REST evidence;
- partial fill/pending blocks another dispatch;
- uncertainty freezes session;
- max one active position invariant executable;
- no automatic POST retry.

Suggested commit:
`feat(execution): reconcile BTC paper lifecycle`

### Task 5 — complete lifecycle-backed EXIT

Implement Push 5.

Primary files:
- strategy application composition;
- BTC risk;
- reconciliation/lifecycle;
- dispatcher;
- lifecycle tests.

Requirements:
- remove application hard-code `holding=False`;
- FLAT => ENTER only;
- HOLDING => EXIT only;
- exit quantity bound to reconciled linked exposure;
- existing high-water and risk rules unchanged;
- no new entry until authoritative FLAT;
- no pyramiding.

Build the full deterministic fake-broker round trip before any real PAPER soak.

Suggested commit:
`feat(application): run BTC paper entry exit lifecycle`

### Task 6 — restart recovery

Implement Push 6.

Primary files:
- session startup;
- journal replay;
- reconciliation;
- ownership/recovery tests.

Requirements:
- restore counters and state;
- fresh broker cut before actionable interval;
- no duplicate intent/client POST;
- budget survives restart;
- unresolved remains blocked;
- explicit resume behavior retained.

Test all documented crash boundaries.

Suggested commit:
`feat(application): recover bounded BTC paper session`

### Task 7 — session evidence and supervised soak readiness

Implement Push 7.

Primary files:
- session evidence/reporting;
- runbook/status/roadmap;
- integration tests.

Requirements:
- deterministic sanitized session summary;
- decision/reason counts;
- order/fill/lifecycle counts;
- stop/halt reason;
- artifact hashes;
- no credentials;
- update canonical STATUS with exact implemented limits;
- operator procedure for relay + PAPER stream + session.

Do not claim profitability or proof-grade fee accounting.

Suggested commit:
`feat(evidence): summarize BTC paper learning sessions`

## Real-provider acceptance sequence

Only after Tasks 0–7 and green CI:

1. Start shared crypto relay.
2. Confirm relay health.
3. Run read-only market + broker + trading-stream smoke.
4. Run session preflight.
5. Run a short supervised PAPER session with frozen parameters.
6. If NO_SIGNAL, preserve decision telemetry and continue later; do not tune to force a trade.
7. On first ENTER, verify client ID, stream updates, REST reconciliation and position.
8. Keep session running for an organic EXIT.
9. Verify authoritative FLAT before another ENTER.
10. Stop at any uncertainty.
11. Preserve raw artifacts outside Git and commit only sanitized evidence/hashes.

## Prompting note

Do not paste this entire file into each Codex request. Point Codex to the exact Task heading plus the active plan section and relevant source files. The durable repository docs are the source of truth; each chat prompt should be short, issue-shaped, and implementation-specific.
