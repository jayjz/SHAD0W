# Architecture

SHAD0W is a Python modular monolith with explicit typed boundaries between research evidence, trading proposals, risk authority, durable execution state, provider adapters, and evaluation.

The architecture is optimized for reconstructability before throughput: an external effect should be explainable from immutable evidence, and missing or contradictory evidence should reduce authority rather than increase it.

For the current implementation snapshot, see [STATUS.md](STATUS.md).

## System shape

```mermaid
flowchart TB
    subgraph Research["Deterministic research lane"]
        H["Historical / frozen market evidence"] --> V["Validation + provenance"]
        V --> F["Deterministic features"]
        F --> S["Strategy hypotheses"]
        S --> T["Chronological simulation"]
        T --> X["Execution economics"]
        X --> E["Evaluation / sealed study evidence"]
    end

    subgraph Market["Live BTC market-data lane"]
        A["Alpaca crypto/us"] --> R["Local crypto relay"]
        R --> M["Normalized BTC events"]
        M --> JME["Hash-chained market evidence"]
        JME --> HF["Completed hourly intervals"]
        HF --> BTF["BTC trend / momentum / volatility"]
    end

    subgraph Paper["Bounded BTC PAPER authority lane"]
        BTF --> P["Strategy proposal or abstention"]
        P --> Q["Independent BTC risk"]
        Q --> D["Durable admission / dispatch journal"]
        D --> B["Alpaca PAPER adapter"]
        B --> O["Orders / fills / fees / positions"]
        O --> C["Strict reconciliation"]
        C --> L["Operational lifecycle"]
        L --> P
    end

    V -. typed domain contracts .-> M
    E -. descriptive authority only .-> Q
```

Research evaluation cannot authorize a broker submission. Strategy cannot bypass risk. Risk cannot manufacture broker state. Broker responses do not become operational truth until normalized evidence is reconciled against the durable journal.

## Authority flow

```mermaid
flowchart LR
    OBS["Observed evidence"] --> FEAT["Deterministic measurements"]
    FEAT --> PROP["Strategy proposal"]
    PROP --> RISK["Risk authorization"]
    RISK --> COMMIT["Durable commit"]
    COMMIT --> POST["Single guarded broker POST"]
    POST --> READS["Broker reads / activities"]
    READS --> RECON["Reconciliation"]
    RECON --> STATE["Operational state"]

    STATE -. next decision context .-> PROP
    RISK -. cannot submit directly .-> POST
```

The durable commit precedes the external POST. An uncertain outcome is not retried automatically. Deterministic client IDs aid recovery and identity; they do not create authority to send again.

## Operational lifecycle

The bounded BTC path uses broker evidence to project one of six states:

```mermaid
stateDiagram-v2
    [*] --> FLAT

    FLAT --> ENTRY_PENDING: committed BUY not yet reconciled
    ENTRY_PENDING --> HOLDING: linked entry + net position agree
    HOLDING --> EXIT_PENDING: committed linked SELL not yet reconciled
    EXIT_PENDING --> FLAT: linked closure + position agree

    FLAT --> UNRESOLVED: incomplete broker evidence
    ENTRY_PENDING --> UNRESOLVED: uncertain / missing submission evidence
    HOLDING --> UNRESOLVED: inventory or fee proof incomplete
    EXIT_PENDING --> UNRESOLVED: uncertain / incomplete closure

    UNRESOLVED --> HALTED: invariant violation
    FLAT --> HALTED: contradictory / foreign state
    HOLDING --> HALTED: contradictory / foreign state
```

`UNRESOLVED` preserves uncertainty. `HALTED` represents a condition that must not grant new dispatch authority.

## Core modules

| Area | Responsibility |
| --- | --- |
| `domain` | Provider-neutral market and evidence value types |
| `data` | Validation, deterministic identity, frozen data boundaries |
| `features` | Deterministic calculations and availability propagation |
| `strategies` | Explicit hypotheses; proposal authority only |
| `simulation` | Chronological eligibility and simulated lifecycle |
| `evaluation` | Trade reconstruction, descriptive metrics, study evidence |
| `risk` | Independent authorization/rejection boundaries |
| `execution` | Broker contracts, journal, reconciliation, dispatch |
| `adapters` | Alpaca and other external translations |
| `application` | Bounded composition and CLI entry points |
| `operations` | Read-only operational/timing evidence utilities |

Provider SDK objects, HTTP payloads, dataframe types, and LLM response objects do not cross into the provider-neutral core.

## Time and causality

SHAD0W models at least:

- observation time — when the market event occurred;
- availability time — when the system could legally consume it;
- decision time — when strategy/risk evaluation occurred;
- dispatch deadline — latest legal external-effect boundary;
- broker evidence observation/availability — when operational facts were observed and usable.

A later receipt cannot make an earlier observation causally newer. A completed interval cannot be used before it exists. Provider timestamps that appear to be in the future relative to system receipt fail closed; no tolerance is silently added to preserve a trade.

## Market evidence

The BTC path separates:

1. bounded historical raw trades for warm start;
2. newly observed live trades/quotes from the local relay;
3. reconstructed completed hourly intervals;
4. deterministic strategy features;
5. durable decision/abstention evidence.

Historical context can establish features but cannot by itself trigger a fresh entry. The current partially captured interval is not actionable.

## Journal and ownership

The execution journal is SQLite-backed and account/scope bound. Local account ownership is bound to one journal path so a fresh empty journal cannot be used to erase unresolved execution history.

Important properties:

- account identity and operational scope are immutable;
- committed attempts are durable;
- client-order identity is deterministic;
- submission and reconciliation are explicit transitions;
- one account owner is permitted within the supported local deployment boundary;
- restart replays/reconciles rather than resetting authority;
- journal loss, replacement, or conflicting identity fails closed.

The local lock is an operational single-host guard, not distributed or hostile-process security.

## Reconciliation

Broker truth is reconstructed from typed account, asset, order, fill/activity, and position evidence.

The reducer does not assume:

- acceptance means filled;
- a position snapshot proves order history;
- absence from one open-orders response proves a submission never occurred;
- gross fill quantity equals net crypto inventory after fees;
- elapsed time resolves uncertainty;
- a deterministic client ID permits retry.

The bounded BTC session additionally requires explicit available BTC to match reconciled net exposure before a SELL can be authorized.

## Research vs operational state

SHAD0W contains two lifecycle concepts and they must not be conflated:

- **simulation lifecycle** — deterministic research state derived from simulated execution outcomes;
- **operational lifecycle** — broker-authoritative state derived from durable attempts and real PAPER evidence.

Research results cannot be imported as broker state. Operational state cannot rewrite historical research evidence.

## Deliberate non-requirements

The current system does not require Kafka, Redis, Celery, Kubernetes, microservices, a distributed database, or a web frontend. Additional infrastructure should be introduced only when a measured deployment requirement justifies it.

Likewise, SHAD0W does not currently implement live-capital support, continuous repeated trading, dynamic leverage, portfolio optimization, or LLM execution authority.
