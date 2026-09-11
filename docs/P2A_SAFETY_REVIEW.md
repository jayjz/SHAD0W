# P2A adversarial operational safety closure

Review date: 2026-09-11. Starting branch: `p2a-paper-risk-gate` at
`b4be9a2fb20797bec4d5f7ba94c0aee707c7b078`. Fetched `origin/main` and local
`main` were both `690c09ecf7a093f150ed05e771fc99b8d09f164f`; the fetched
P2A branch matched the starting HEAD. The worktree was clean.

Disposition: suitable for merge as a bounded, process-local admission laboratory
after the fixes below. It is not safe for external paper submission or continuous
trading. P4A shadow-only live-data observation remains the next proposed milestone;
this review implements no P4A or broker work.

## Reproduced defects and fixes

1. **Reservation reference substitution concealed inconsistent state.** Admit a
   SPY BUY, then supply its grant reference on a QQQ outstanding BUY. A distinct
   SPY opportunity could receive a second grant because `_effective_state` removed
   the original reservation based only on reference equality. Changed quantity,
   side, or intent identity under the reference also escaped inconsistency checks.
   Four regressions failed with an unexpected grant. The merge now deduplicates
   complete equal orders only; conflicting evidence reaches the existing evaluator
   and rejects. No new reconciliation protocol or reservation-release path exists.
2. **Contradictory source-rule evidence authorized.** A matching feature alone
   sufficed even when an entry claimed an exit reason, the observed value did not
   satisfy the declared threshold, or the feature was already too old under the
   signal's own freshness allowance at signal creation. Six regressions, covering
   entry and exit, failed with `AUTHORIZED`. Risk now verifies those internal
   assertions and rejects them as `lineage_mismatch`. It does not generate strategy
   proposals, size them, authenticate configurations, or reconstruct market history.

All ten regressions were run and observed failing before the implementation edits,
then passed after the fixes. The risk implementation identifier advances to
`shadow.risk.paper.v2` because authorization/evidence semantics changed. Intent
identity encodings are unchanged.

## Identity and terminal decisions

No collision was found between distinct, consistently declared opportunities.
Identity means one source-feature opportunity per strategy/configuration, direction,
instrument, and operational scope. It does not mean every consumer evaluation or
every hypothetical position cycle on the same feature.

| Field | Business identity treatment |
| --- | --- |
| Operational scope; instrument | Included |
| Strategy ID/version; configuration ID | Included |
| Signal direction | Included |
| Feature name/input/version/window | Included |
| Feature observation and availability times; dataset ID | Included |
| Signal decision/availability time | Payload only; signal contract requires these times to be equal |
| Feature value; entry/exit thresholds; maximum feature age; signal reason | Payload only; changes under the same source/config identity conflict |
| Operational quantity and quantity configuration | Payload only; changed sizing cannot create a fresh source opportunity |
| Risk consumer time, current policy, current state | Decision evidence only |

A genuinely changed configuration needs a new configuration identity. Tests generate
valid entry and exit signals using the existing strategy with distinct configuration
IDs and confirm distinct authorizable identities. Numerically equal Decimals retain
the same identity/payload; source evaluation delay cannot create a second identity.
Feature availability is source lineage, not consumer receipt/retry time.

Dataset/configuration IDs remain caller declarations. Signal-versus-feature
changes in value, dataset, implementation, availability, and window reject. Quantity
configuration instrument mismatch cannot construct an intent. Consistently changing
both a signal and its supporting evidence cannot be authenticated as tampering by
P2A; there is no trusted configuration or dataset registry here.

Terminal-rejection conclusion: **retain the intended one-shot invariant**. Incomplete
inventory followed by complete inventory, stale state followed by fresh state, and
disabled trading followed by enablement all leave the original rejection authoritative.
This forfeits that source opportunity without asserting that a trade occurred.
Recovery requires a new source opportunity. Adding retryable dispositions is not
necessary for the bounded P2A safety claim and would change its liveness contract.

## Operational limits tested and retained

- **Policy:** immutable and fixed per gate; there is no supported update API. Pure
  evaluation under a changed policy can differ, but even private policy replacement
  does not revive history or revoke an issued claim. A replacement gate loses
  history and cannot serve as a safe policy-update/retry mechanism.
- **Controls and expiry:** claims accept no current controls, policy, state, quote,
  or time and have no expiry. Later kill-switch activation or trading disablement
  rejects new admissions but leaves an earlier claim consumable. Evidence can be
  stale a day later while that claim still succeeds. Contracts and API docstrings
  now explicitly limit the claim to admission-time authorization; it is insufficient
  for external dispatch.
- **Reservation lifecycle:** complete equal echoes count once; changed references
  or contents fail closed. Claimed and abandoned grants retain reservations. A
  reported fill does not reconcile them: BUY plus a position, or SELL without a
  position, rejects as inconsistent. Initial known positions can receive full-exit
  grants, but exits do not free capacity. Hypothetical broker rejection has no P2A
  handler and cannot release capacity. Continuous entry/fill/exit operation is absent.
- **State identity:** IDs/revisions are evidence labels, not a monotonic update
  protocol. Reused labels with changed contents produce distinct fingerprints.
  Duplicate position IDs, outstanding identities/references, and per-instrument
  conflicts reject even elsewhere in scope. Reservation merging preserves supplied
  times and inventory completeness and cannot refresh stale or incomplete state.
- **Timing:** exact maximum age authorizes, while maximum plus one microsecond
  rejects for signal, feature, quote, state, and controls. Recent availability cannot
  refresh an old observation. Future observation or availability rejects; independent
  future signal availability is already prohibited by the upstream signal contract.
- **Concurrency:** barrier-synchronized same-intent, same-instrument/different-intent,
  final-slot, exit-versus-entry, and conflicting-payload admissions produce exactly
  one grant/reservation where appropriate. Concurrent claims have exactly one winner.
  Lock scheduling can select either valid competing entry; the guarantee is atomic
  capacity enforcement, not deterministic OS thread ordering.
- **Forgery/replay:** raw signals, intents, lifecycle actions, economic executions,
  decisions, dictionary evidence, deep copies with different tokens, changed grant
  payloads, and grants from another gate cannot claim. A copied private issuer/token
  can construct an equivalent artifact through Python introspection, but it shares
  the original one-use record. Private mutation of the intent payload fails claim
  validation. This is application API authority, not hostile-process security.
- **Quantity:** 5, 5.0, and 5.000 are equivalent; zero, negative, and fractional
  quantities reject; NaN/infinities cannot enter the contracts; a finite `1E1000`
  quantity rejects above policy. Hostile precision, rounding, exponent limits, and
  Decimal traps do not alter identities or decisions. Whole shares remain P2A-only.
- **Kill switch:** an automation admission freeze for both entries and exits. It
  neither closes positions, reduces exposure, nor revokes existing claims.
- **Dependencies:** the complete branch diff introduces no runtime dependencies,
  broker SDK, Alpaca integration, network I/O, ambient clock, environment/filesystem
  lookup, database, persistence, or global mutable trading state. Gate state is
  instance-local; the module issuer sentinel is an immutable identity token. Tests
  use synthetic inputs and require no external service.

## Verification

The original focused suite passed 45 tests before adversarial additions. The ten
defect regressions then failed as described above. Final focused tests pass **139**
cases; the complete repository passes **330** tests. Ruff lint passes, Ruff format
check reports **61 files already formatted**, mypy reports no issues in **47 source
files**, and `git diff --check` passes.

All four requested `uv run` commands were attempted and exited 127 with
`/bin/bash: line 1: uv: command not found`. Verification instead used the existing
isolated `/tmp/shadow-p05b-venv/bin/` Python/pytest, Ruff, and mypy toolchain
(Python 3.12.3; pytest 9.1.1). No `uv` success is claimed.

The review covered all risk source, the full original risk tests, AGENTS.md, ADR
0004, risk/architecture/data contracts, and every file in the branch diff. Upstream
domain, feature, strategy, simulation, and execution contracts/implementation were
inspected to trace source identity, chronology, and research-versus-operational
authority. Changes are confined to P2A implementation, regression evidence, and
the documentation of its claims.

## Blockers before Alpaca paper submission

External submission requires separate P5A authorization and a broker adapter, an
authoritative provider-to-domain mapping and fresh operational state, durable
idempotency with restart recovery and one scope owner, and reconciliation for
submission failure/uncertainty, orders, fills, cancellation, positions, and reservation
release. It also requires explicit submission-time policy/control revalidation and
bounded freshness including the revalidation-to-dispatch race. Broker instrument,
quantity, account/buying-power, and order eligibility must be established; the current
gate has no authoritative account model. P4A shadow-only data work can precede these
requirements but does not remove them or authorize orders.
