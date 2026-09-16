# P5A execution plan

P5A.0 delivers the [execution contract](P5A_EXECUTION_CONTRACT.md),
[ADR 0005](decisions/0005-paper-execution-recovery.md), reconciled milestone
documentation, and credential-free Python 3.12 CI. Broker execution is unimplemented.
The following are local follow-up tickets, not published GitHub issues or permission
to run a canary. Each implementation assignment must explicitly select its slice;
network activation is reserved for P5A.8 after operator approval.

## Dependency order

| Ticket | Depends on | Bounded deliverable |
| --- | --- | --- |
| P5A.1 | P5A.0 | Provider-neutral operational contracts and fake broker |
| P5A.2 | P5A.1 | Durable journal, stable identity, and exclusive ownership |
| P5A.3 | P5A.1, P5A.2 | Reconciliation and lifecycle projection from recorded broker evidence |
| P5A.4 | P5A.2, P5A.3 | Durable risk admission and guarded fake dispatch |
| P5A.5 | P5A.1, P5A.3, P5A.4 | Alpaca paper adapter verified with fake transport |
| P5A.6 | P5A.3, P5A.4, P5A.5 | Separate paper application and lifecycle-backed signals |
| P5A.7 | P5A.6 | Crash/recovery acceptance suite and operator runbook |
| P5A.8 | P5A.7, operator approval | Bounded supervised paper canary and evidence review |

All tickets retain existing P2A/P4A behavior, update their relevant contracts, and
run the canonical checks. Synthetic tests require no credentials or external
broker access. No ticket authorizes live endpoints, live capital, strategy threshold
changes, or profitability claims. Proposed filenames below are implementation
boundaries; a ticket may use adjacent small modules without expanding its scope.

## P5A.1 — Typed broker evidence and canary configuration

Scope: add `src/shadow/execution/broker.py` with immutable provider-neutral account,
clock, asset, position, order, fill/update, completeness, submit-result, and error
contracts plus the broker protocol. Add a canary configuration separate from
research economics and P4A configuration. Add `tests/test_broker_contracts.py` and
a deterministic fake broker for later tickets.

Acceptance: explicit paper-only target, one symbol/share, market/DAY, regular hours,
daily limit and freshness/deadline requirements validate without defaults that
enable trading. Unknown statuses, missing inventory, malformed Decimal/time values,
ambiguous submission, partial fills and replacement links are representable or
fail closed. No provider models cross the port. No networking, SDK, journal, or CLI.

## P5A.2 — Durable identity, journal, and ownership

Scope: add `src/shadow/execution/journal.py`, schema/version handling, stable
account/scope and source-opportunity mapping, deterministic client-ID mapping, and
exclusive process ownership. Use standard-library SQLite and supported local
storage. Persist immutable transitions plus transactional projections; do not
reuse P4A JSONL as a trading ledger. Document durability and supported locking.

Acceptance in `tests/test_execution_journal.py`: reopen/replay equality; same
opportunity across sessions/reconnects produces one identity; changed payload or
client-ID collision halts; first rejections remain terminal; journal/account/config
mismatch, corruption, failed commit, disk-full and second owner cannot dispatch.
Transaction rollback cannot expose a capability. Durable daily attempts survive
restart/date rollover; uncertain attempts are not refunded. No broker calls or
automatic storage recreation/migration that discards evidence.

## P5A.3 — Broker reconciliation and reservation lifecycle

Scope: add `src/shadow/execution/reconciliation.py` and deterministic lifecycle
reducers over the port/journal. Startup, disconnect, uncertain attempts, and
operator intervention require complete reconciliation. Implement bounded reads,
pagination/coverage validation, overlapping stream/snapshot reconciliation,
order/client lookup, replacement chains, and evidence-backed capacity transitions.

Acceptance in `tests/test_paper_reconciliation.py`: full and partial entry/exit,
late/duplicate/out-of-order fills, terminal rejection/cancellation/expiration,
cancel/replace races, missing successors, trade corrections/busts, unlinked manual
activity, and contradictory snapshots never silently free capacity. One 404 or
empty open-order response cannot resolve uncertainty. Restart restores linked
holdings only with broker agreement. Fractional terminal residue and unavailable
history halt. Repeated evidence is idempotent and projections replay deterministically.
Only fake broker reads; no submission or P2A private-state edits.

## P5A.4 — Durable independent risk authority and dispatch guard

Scope: add `src/shadow/risk/paper_authority.py` and
`src/shadow/execution/dispatch.py`. Reuse P2A's pure rules through an explicit
versioned composition; preserve its existing gate. Add durable terminal admission,
atomic reservation, one-use local capability, broker/account/canary checks,
submission-time revalidation, and commit-before-send ordering against a fake port.
Define exact own-reservation accounting and journal/control revision comparisons.

Acceptance in `tests/test_paper_dispatch.py`: raw signals/intents/decisions and
reconstructed grants cannot dispatch; concurrent candidates consume one slot;
stale inputs, disabled controls, closed/near-close market, buying-power failures,
ownership loss and expired deadlines stop the send. Inject control changes between
revalidation/commit/transport and verify the documented local ordering and residual
race. Broker acceptance followed by timeout persists uncertainty, keeps reservation
and daily count, and prevents every later submit. Crash before/after each durable
boundary cannot enable automatic retry. Check entry budget leaves an exit slot.
No real transport, network, credential handling, or autonomous cancel/replace.

## P5A.5 — Alpaca PAPER adapter with offline conformance tests

Scope: add distinct `src/shadow/adapters/alpaca/paper.py` and paper trade-update
normalization. Recheck official endpoint/field/event contracts. Select and justify
the smallest required transport dependency in this ticket; P5A.0 adds none. Fix
paper origins, reject redirects, disable submission retries, sanitize diagnostics,
and map provider errors to definitive or uncertain outcomes conservatively.

Acceptance in `tests/test_alpaca_paper.py`: fake HTTP/WebSocket transcripts verify
account/clock/assets/positions, paginated orders, client-ID lookup, submit payload,
trade updates, and all unknown/error cases. Paper and P4A credentials/configuration
are separate; arbitrary/live origins and account mismatches reject. No raw provider
types escape, and the client-ID mapping fits verified provider limits. No credentials,
trading endpoint connection, or order submission during implementation or CI.

## P5A.6 — Separate paper application and operational signals

Scope: add `src/shadow/application/paper.py` and a distinct disabled-by-default
paper command. Compose the previous tickets with reusable normalized market data
and existing feature/strategy functions. Bind stable source/config identity across
restarts; derive strategy context from reconciled operational lifecycle. Include
bounded run duration, explicit arming, evidence paths, controls, and halt reporting.

Acceptance in `tests/test_paper_application.py`: a fake entry/fill/exit/flat cycle
uses independent risk at both submissions. Pending state suppresses new proposals;
restart with a linked holding can warm fresh data and later propose its threshold
exit. No old-signal replay, synthetic liquidation, or session-label identity bypass.
P4A banner/command/import boundary remains market-data-only. Budget, stale data,
unknown broker state, disconnect, kill switch and shutdown behave as contracted.
All integration runs use fakes; no actual account access or order submission.

## P5A.7 — Recovery acceptance and runbook

Scope: add an end-to-end fault-injection suite and `docs/P5A_CANARY_RUNBOOK.md`.
Exercise abrupt process termination against a fake broker and durable temporary
journal at admission, dispatch marker, broker acceptance, response persistence,
fill projection and capacity release. Document setup, supervision, halt reasons,
read-only recovery, resume gates, journal retention and exposure left at shutdown.

Acceptance: timeout-after-acceptance recovers the original order with no second
POST; dropped/duplicated updates and restart at every boundary converge or remain
explicitly halted. Test second owner, corrupt/rolled-back journal, broker account
reset, daily rollover/early close, fractional residue, replacement and unknown
activity. Complete all canonical checks and a full diff/safety review. Record
limitations and the remaining external race. No external canary yet.

## P5A.8 — Supervised paper canary, separately approved

Scope: execute the reviewed runbook only after P5A.1–P5A.7 pass and an operator
approves the exact manifest: dedicated paper account binding, single liquid equity,
unchanged strategy configuration, one share, daily ceiling, buying-power buffer,
freshness limits, dispatch deadline/close guard, duration, storage, supervisor and
recovery procedure. Provision secrets outside Git/CI only under that assignment.

Acceptance: reconcile before arming, retain sanitized broker/journal evidence,
stop at any unknown state, and reconcile after the bounded run. Report actual
submissions, fills, open exposure, recovery outcomes and limitations. If thresholds
produce no signal, report no trade; never alter thresholds or force an order to
obtain a demonstration. No promise of a round trip or profitable outcome. Closure
requires broker agreement or an explicit unresolved halt and operator handoff;
unresolved state blocks further activation. No live-capital gate is implied.
