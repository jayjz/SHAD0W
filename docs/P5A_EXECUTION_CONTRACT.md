# P5A paper execution contract

Status: accepted design; **P5A.0 contains documentation and CI only**. Requirements
below are future acceptance criteria, not claims of implemented safety. P2A and
P4A remain complete within their existing bounds. Paper execution does not validate
the strategy or establish profitability. Live-capital trading is prohibited.

## Authority and executable envelope

Signals propose; independent risk authorizes; execution obeys; broker
reconciliation establishes operational truth. Research fills, P4A candidates,
historical decisions, and P2A claims cannot authorize external submission.

The first executable canary must enforce all of the following:

- One dedicated Alpaca paper account, one persistent operational scope, one local
  process owner, and one explicitly allowlisted liquid US equity selected by the
  operator. No concurrent manual trading or other bot may use that account.
- Exactly one whole share per submitted order; BUY opens one long, SELL fully
  closes that same journal-linked one-share holding. No shorts, additions,
  fractional submissions, partial exits, leverage strategy, or other assets.
- Simple market/DAY orders, extended hours disabled, regular market hours only
  using fresh broker clock/session evidence including holidays and early closes.
  A configured pre-close guard must exceed the dispatch deadline and allowed clock
  skew. Market orders do not guarantee price, immediate fill, or same-day closure.
- At most one concurrent position and one outstanding intent/order. Filled entry
  quantity plus its unfilled remainder occupies the same single capacity slot.
- A required finite positive daily submission ceiling, persisted per paper account
  and broker trading date. Count every committed dispatch attempt, including
  uncertain and rejected submissions, without refunds or restart resets. Both
  entry and exit count. Entry requires room for its attempt and one later exit;
  this preserves budget capacity but cannot guarantee exit feasibility.
- Explicit enablement, current controls, freshness limits, account eligibility,
  asset tradability, a positive quote, and sufficient broker buying power for entry.
  A declared conservative notional estimate/buffer is an eligibility check, not a
  guaranteed market-order price ceiling. The broker can still reject the order.
- Any missing, unknown, stale, incomplete, inconsistent, or out-of-envelope broker
  state halts new submissions account-wide, including automated exits. Reads and
  evidence collection may continue. Halt never means flat or liquidated.

The exact symbol, account binding, limits, freshness/deadline/close-guard values,
buying-power buffer, run duration, and escalation procedure require operator
approval in a versioned run manifest before activation. There are no enabled
defaults. No order, account request, credentials, SDK, or HTTP dependency is added
in P5A.0. The P4A `shadow-live-data` command remains market-data-only.

## Provider-neutral boundary and endpoint separation

The future broker port accepts immutable typed requests and returns typed
observations/results. Decimal quantities/prices, UTC source and receipt times,
account/scope binding, evidence references, completeness, and explicit unknown
states cross the boundary. SDK objects, HTTP responses, and provider JSON remain
inside the adapter. Malformed fields and unrecognized statuses cannot default to
flat, filled, canceled, or safe to retry.

The port covers account eligibility/buying power, broker clock/session boundaries,
asset eligibility, complete positions, paginated open/recent orders, order lookup
by broker ID and client ID, trade updates, and one guarded submit operation.
Cancellation/replacement observations are required; automated cancel/replace
commands are outside the initial canary. Operator intervention is reconciled and
recorded before resumption, never treated as permission to ignore broker evidence.

The Alpaca paper adapter fixes the HTTPS origin to
`https://paper-api.alpaca.markets` and trade-update origin to
`wss://paper-api.alpaca.markets/stream`. It exposes no arbitrary base URL, live-mode
boolean, live credential fallback, or redirect to another origin. Credential
configuration is separate from P4A data credentials. Tests use injected fake
transports, not a configurable production endpoint. Verify the expected account
binding on every recovery; mismatches halt. Do not log credentials or commit real
account identifiers or private captures.

These origins and the distinction between paper and live environments follow
[Alpaca paper trading documentation](https://docs.alpaca.markets/us/docs/paper-trading)
and [trade-update streaming documentation](https://docs.alpaca.markets/us/docs/websocket-streaming).
Provider details must be checked again when implementing the adapter.

For Alpaca PAPER, the persistent SHAD0W account binding is explicitly the
human-facing `account_number`; it is not interchangeable with the provider's
UUID-shaped `id`. Every account read must require both values, verify
`account_number` exactly against the configured binding, and may preserve `id` as
provider identity evidence. Missing, malformed, or mismatched fields halt.

## Durable identity and journal

Use a local SQLite journal through the standard library, with an exclusive process
ownership lock held through network operations. Transactions alone do not prevent
two dispatchers after a database lock is released. Unsupported storage/locking,
lock contention, corruption, disk-full, or failed durable commit halts dispatch.
Use foreign keys, unique constraints, full durability settings, and a documented
schema version/migration policy. Network filesystems and multi-host operation are
outside this design. Never delete, recreate, or roll back a journal to recover
capacity; lost/restored/stale storage requires operator recovery and reconciliation.

Ownership is account-wide: one dedicated paper account has one authoritative local
journal and one owner lock in a fixed local ownership directory. Changing scope or
journal path cannot create a second owner for that account. The Linux advisory lock
is a local operational guard, not distributed coordination or hostile-process
security; shared/network filesystems, multi-host operation, and forked dispatch are
unsupported. PID data is diagnostic only. Never delete a lock file to clear a
stale owner. A journal cannot prove an undetectable rollback of its whole storage
image, so a restored or stale image requires operator recovery and reconciliation.
Storage errors that prevent a durable record must immediately prevent dispatch;
logs or process memory cannot substitute for the missing record.

Persist append-only transition evidence and transactionally maintained projections:
run/configuration/code identity; stable account binding and scope; market/feature/
signal lineage; full intent and payload fingerprint; first admission decision;
policy/control versions; broker evidence and reconciliation revision; reservations;
client/broker order IDs; attempts, deadlines and daily counters; statuses, cumulative
fills, execution IDs, prices and quantities; replacement links; halts and recovery
dispositions. Credentials are excluded. Projection replay must reproduce decisions
and capacity from recorded inputs, without network access or ambient clock reads.

Preserve P2A business identity and full-payload conflict detection. A rejected source
opportunity remains terminal across restart. Exact redelivery cannot issue another
capability; changed content under an existing identity halts as a conflict. Scope,
strategy/configuration identity, and source dataset lineage must be stable across
process sessions: P4A's session-derived dataset/configuration labels cannot be used
unchanged for executable identity. Record a stable source-opportunity key and bind
its first canonical feature/availability evidence so a reconnect, new receipt time,
or session name cannot manufacture another opportunity from the same source bar.
Configuration changes are explicit migrations, never a retry mechanism.

The source key is canonical causal identity: stable account/scope and feed or
dataset lineage, instrument, completed-bar observation identity, strategy
identity/version, and feature identity/version/window. Session IDs, reconnect
counters, receipt times, and mutable display labels are excluded. The first
feature/signal/intent binding under that key is immutable; a material variant is a
conflict and halts. An intact journal record proving no `dispatch_started` was ever
committed is distinct from a potentially-submitted attempt. Any committed attempt,
including one recovered with no durable response, is permanently spent and
uncertain until broker-authoritative reconciliation; missing broker history does
not prove that it was not submitted.

The Alpaca mapping is versioned and deterministic:

```text
client_order_id = "shp1_" + sha256(canonical_json([
    "shadow.alpaca.paper.client-order.v1", stable_account_binding,
    operational_scope, intent_identity
])).hexdigest()[:40]
```

Canonical encoding uses UTF-8, compact JSON array separators, fixed field order,
JSON ASCII escaping (`ensure_ascii=True`), and validated strings without implicit
whitespace or Unicode normalization.
The resulting 45-character identifier is persisted with the full digest and intent
payload before dispatch. Unique constraints reject truncated-hash collisions and
conflicting payloads; an existing ID is never overwritten. Retry time, process ID,
randomness, and session ID are excluded. The adapter must test the mapping against
the provider's then-current field limits before activation.

Alpaca supports [lookup by client order ID](https://docs.alpaca.markets/us/reference/getorderbyclientorderid).
This is a reconciliation key, not proof that repeating a POST is safe. The canary
permits at most one network submit invocation for an attempted intent; it makes
no exactly-once network-delivery or broker-execution claim.

## Admission, revalidation, and dispatch ordering

P5A adds a versioned durable risk authority. It preserves the P2A pure evaluator's
rules and terminal source-opportunity decisions, with additional broker/canary
checks. It does not edit P2A gate internals or revive its private grant tokens.

1. Start disabled, acquire sole ownership, verify journal integrity/account/config
   binding, and reconcile complete broker state. Only a committed, fresh,
   consistent reconciliation revision can make the application ready.
2. Serialize candidate admission against controls, reconciliation, and other
   candidates. Evaluate risk from explicit causal inputs. Atomically commit the
   first decision, intent/client identity, and reservation before exposing a
   process-local capability. Rejections persist without reservations.
3. Immediately before dispatch, revalidate the same intent against fresh feature,
   signal, quote, account, asset, clock, complete position/order state, policy,
   controls, daily budget, and owner health. Check observation age and availability
   for each input; recent receipt cannot freshen old data. Reconcile gaps first.
   Revalidation identifies exactly its own unsubmitted reservation for capacity
   accounting; it must retain every unrelated reservation/order and reject any
   identity/content mismatch. Passing a reservation-free fabricated state to risk
   is not a supported workaround.
4. In a durable transaction, compare the expected reconciliation/control/policy
   revisions, consume the one-use capability, commit `dispatch_started`, the exact
   request, revalidation evidence, deadline, and incremented daily attempt count.
   Failure or an uncertain commit means no network call and halt.
5. After commit, perform a final local revision/kill-switch/deadline check at the
   transport seam. Any change, expired input, lost ownership, or scheduling delay
   beyond the bound prevents sending and halts. Otherwise invoke submit once with
   transport retries and redirects disabled, bounded by a monotonic deadline.
6. Persist a valid broker acknowledgement, rejection, or explicit `uncertain`
   result. Acknowledgement establishes an order, never a fill. Reconcile before
   any new intent; failure to persist a response halts and recovers via broker
   lookup using the already committed client ID.

All pre-dispatch failures leave durable evidence and no reusable capability.
Reservations on abandoned admissions remain blocked pending authoritative broker
reconciliation proving no order/exposure; local timeout or abandonment alone
cannot release them. If that proof is unavailable, stay halted.

### Revalidation-to-network race

One serialized dispatcher establishes a local ordering for control changes and
the final send check. Record UTC evidence times plus monotonic elapsed time;
backwards clocks or excessive broker/local skew halt. No queue or unbounded await
may intervene after the final check. The dispatch deadline must remain within all
input validity windows and the regular-session close guard.

The broker and network do not participate in the journal transaction. A kill
switch, market close, account restriction, fill, or disconnect can occur after
the final check or while a request is in flight. This residual race cannot be
eliminated locally. A control change then freezes subsequent submissions and
triggers reconciliation; it cannot promise revocation of an accepted order.

### Uncertainty and retries

A timeout after broker acceptance, disconnect during send/response, malformed
response, ambiguous server error, or recovered `dispatch_started` with no durable
result enters `uncertain`. Retain capacity and daily usage; halt all new submissions.
Do not label a transport exception as rejection or change client ID to try again.

Reconciliation queries the original client ID, known broker ID, complete order
history covering the attempt, positions, account, and trade updates. A single 404,
empty open-order list, elapsed DAY session, or absent stream event proves nothing
about acceptance or fills. Bounded read retries may run with backoff; exhausted
budgets leave the application halted. Only matching authoritative evidence can
resolve acceptance/rejection/terminal status and associated exposure. Inconsistent
or insufficient evidence stays unresolved and requires operator investigation.
Historical execution evidence that the provider cannot cover remains unresolved;
P5A.3 must not infer missing fills, prices, or absence from a snapshot or stream.

Reconciliation precedes any consideration of retry. The canary deliberately has
**no automatic POST retry**, even if absence seems established. A resolved attempt
remains spent; future trading requires a distinct source opportunity, fresh risk
authorization, consistent broker state, and an explicit resume after a halt.

## Broker lifecycle and reservations

Trade updates are broker evidence, but a stream connection is not a complete
account snapshot. Persist and deduplicate provider execution/event IDs and order
versions; cumulative quantity must not be counted twice. Out-of-order, missing,
contradictory, corrected, or unknown updates trigger reconciliation. Do not impose
a fabricated total ordering on independent REST snapshots and streamed events.

| Authoritative evidence | Required lifecycle and capacity behavior |
| --- | --- |
| Accepted/new/pending order | Retain pending entry/exit reservation; no holding is inferred from acceptance. |
| Partial entry fill | Record actual cumulative exposure and remaining order quantity in the same capacity slot; suppress proposals while remainder is pending. |
| Full entry fill | After order/fill/position agreement, atomically transform BUY reservation into one journal-linked long holding. Only then may strategy receive `HOLDING`. |
| Partial exit fill | Reduce evidenced exposure, retain residual holding and SELL reservation; no flat state or new entry. |
| Full exit fill | Release position capacity only after order/fill evidence and complete broker positions agree that the linked holding is closed. |
| Rejected/canceled/expired | Confirm terminal status and final cumulative fills; release only the unfilled obligation after reconciliation. Any filled quantity remains exposure. |
| Pending cancel or cancel rejection | Retain order/reservation; request receipt is not cancellation and a fill may race cancellation. |
| Pending replace/replaced | Follow predecessor/successor IDs and all fills; retain combined exposure/obligation. An unexpected replacement halts the canary; it never creates free capacity. |
| Unknown, suspended, contradictory, missing successor, trade correction/bust | Halt and reconcile; never map to a successful close or release. |

These categories reflect [Alpaca's order lifecycle](https://docs.alpaca.markets/us/docs/orders-at-alpaca)
and [trade-update events](https://docs.alpaca.markets/us/docs/websocket-streaming);
the table's release and halt rules are SHAD0W design requirements.

Partial fills must be represented accurately even with one-share requests. While
a known order completes, partial quantities keep the application pending. A
terminal fractional residual, quantity above one, unexpected symbol/short/manual
position, unexplained order, or unlinked replacement violates the envelope and
halts automation. Do not round quantities or submit a fractional recovery exit.
Account-wide inventory is required to detect activity outside the allowlist.

Every release/transformation transaction references broker order/fill/position
evidence and the reconciliation revision. No release is justified by a local
claim, elapsed time, shutdown, cancellation request, or a standalone position
snapshot. Repeated evidence must reproduce the same projection without freeing
capacity twice. Late terminal updates and fill corrections remain inspectable.

## Reconciliation and restart recovery

Startup and stream recovery begin halted. Replay and validate the journal, rebuild
terminal decisions, reservations, daily counters, and linked exposure, and locate
every attempted/unresolved order. A crashed process-local capability is not revived.
Unsent admissions are abandoned and reconciled; started attempts are uncertain.
An empty new journal is never evidence that an account is flat.

Subscribe/buffer trade updates and fetch complete account, clock, positions, open
orders, and sufficient recent/terminal order history, resolving pagination. Replay
overlapping updates and repeat affected reads until IDs, cumulative fills, and
positions agree. This yields an explicitly evidenced reconciliation cut, not a
claim of an atomic provider snapshot. If ordering/coverage cannot be established,
remain halted. Reconnect gaps cannot be healed merely by receiving a new quote.

Persist the consistent reconciliation revision before exposing new risk state.
Recover a holding only from journal-linked broker fills/orders plus matching
positions; unexpected holdings require operator investigation. Account resets,
journal loss/rollback, unsupported schema, history outside provider retention,
ownership loss, and irreconcilable updates block activation. Operator approval
cannot substitute for missing broker evidence or authorize journal deletion.

## Live entry and lifecycle-backed exit

The future separate paper application may reuse P4A normalization and deterministic
feature/strategy functions, but it must supply operational state from reconciled
broker lifecycle. Fresh completed bars while reconciled flat may propose entry.
Pending entry/exit, stale feeds, disconnect, or uncertainty suppress submissions.
Reconciled holding may propose the existing threshold exit from fresh features;
risk must authorize SELL of exactly the broker-confirmed one-share linked holding.
No strategy threshold or holding-horizon change is implied.

Restart may recover a holding, warm fresh market data, and later propose its exit;
it cannot replay an old entry or synthesize a close. Session end, daily-budget
exhaustion, or kill switch does not liquidate. The runbook must retain evidence of
open exposure and name an operator recovery procedure for holdings left overnight.

## Verification gate

Before an operator can activate the canary, fake-transport and temporary-journal
tests must cover crash points around every commit/send/response, broker acceptance
followed by timeout, duplicate opportunities across sessions, identity conflicts,
concurrent owner rejection, disk failure, stale controls, the final-send race,
partial/late/corrected fills, cancellation/replacement races, incomplete pagination,
restart with exposure, daily limits across restart/date rollover, and unknown state.
Prove identical recorded inputs reproduce lifecycle/risk projections. Keep P2A/P4A
regressions and all canonical checks passing. CI has no trading credentials and
must never connect to broker endpoints. Implementation tickets and operator gates
are defined in the [execution plan](P5A_EXECUTION_PLAN.md).
