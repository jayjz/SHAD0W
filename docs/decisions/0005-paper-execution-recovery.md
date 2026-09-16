# ADR 0005: Durable paper execution with broker-authoritative recovery

- **Status:** accepted design for P5A; P5A.0 implements documentation and CI only.
- **Context:** P2A grants prove admission once in one process. They do not expire,
  revalidate controls, survive restart, or release reservations. P4A observes flat
  entry candidates without broker inventory. Neither can safely dispatch orders.
- **Decision:** Introduce a separate paper application with provider-neutral broker
  contracts, an independent durable risk authority, and a local SQLite execution
  journal using Python's standard library. One process owns one dedicated paper
  account/scope. Commit identity, decisions, reservations, and dispatch attempts
  before network writes. Reconcile account, clock, positions, orders, and trade
  updates before admitting work, after uncertainty, and after restart.
- **Consequences:** The initial envelope is one allowlisted liquid equity, one
  whole share, market/DAY, regular hours, one concurrent position, and a durable
  daily submission budget. Paper endpoint configuration is structurally separate
  from live endpoints; there is no live implementation or live-mode switch.

The [execution contract](../P5A_EXECUTION_CONTRACT.md) defines the required state
transitions and evidence. SQLite supplies local atomic transactions, not an atomic
transaction with a broker. The journal must use durable commits, integrity checks,
uniqueness constraints, and an exclusive process lock on supported local storage.
No Redis, remote database, worker queue, or distributed owner is required.

Network submission is never described as exactly once. A deterministic Alpaca
`client_order_id` locates broker evidence; it does not make repeated POSTs safe.
Timeout, disconnect, lost response, or crash after the dispatch marker creates
uncertainty. All new submissions halt while reconciliation runs. The canary never
automatically resubmits an attempted intent, even after a not-found lookup.

Submission requires fresh independent risk evaluation plus current account,
clock, controls, and durable capacity checks immediately before dispatch. Local
serialization and a bounded dispatch deadline reduce the revalidation race but
cannot eliminate external changes or revoke an in-flight request. A kill switch
freezes entry and exit automation; it is not liquidation authority.

P5A will introduce a versioned durable risk authority that reuses provider-neutral
intents and pure P2A risk rules. It will not reconstruct P2A's private tokens, reset
its history, mutate its reservations, or interpret its one-use claim as external
submission permission. P2A remains an admission laboratory; P5A adds independently
tested operational lifecycle semantics. Simulation fills never establish holdings.

Alternatives rejected: in-memory duplicate tracking loses history on restart;
P4A's flushed JSONL capture is not an atomic execution journal; retrying a POST
after transport failure risks duplicate exposure; a generic live/paper client
would broaden the authorized surface. Conservative halts sacrifice liveness and
may leave paper exposure open until authoritative recovery or operator action.

Implementation is split into [P5A.1–P5A.8](../P5A_EXECUTION_PLAN.md). This ADR adds
no adapter, persistence code, account connection, order path, or strategy evidence.
