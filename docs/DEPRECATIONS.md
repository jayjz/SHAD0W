# SHAD0W deprecations and compatibility surfaces

This document distinguishes current SHAD0W interfaces from transitional and
historical surfaces retained for compatibility, evidence, or staged removal.

## Active

- `shadow-btc-paper-loop`
  - bounded repeated BTC/USD PAPER soak
  - operational experimental authority
  - not proof-grade fee/accounting authority

- `shadow-btc-paper-session`
  - bounded strict BTC PAPER session
  - proof-oriented reconciliation and lifecycle

- `shadow-crypto-paper`
  - BTC PAPER preflight and bounded experiment plumbing

- `shadow-crypto-capture`
  - crypto market-data capture

- `shadow-crypto-feed-relay`
  - shared Alpaca crypto feed relay

- `shadow-crypto-timing-observe`
  - diagnostic market-data timing observer

- `shadow-live-data`
  - read-only legacy/equity live-data path
  - retained pending separate audit

## Retired application entrypoints

- `shadow-paper-prepare`
  - original generic/equity PAPER preparation application
  - retired during repository convergence
  - historical workflow preserved in `docs/P5A_CANARY_RUNBOOK.md`
  - underlying risk/journal primitives remain active where independently used

## Transitional removal candidates

- `shadow-paper-canary`
  - original generic/equity PAPER canary application
  - superseded at the application layer
  - generic dispatch primitives remain independently tested

## Shared primitives that remain active

The following predate the BTC-specific execution path but remain shared
infrastructure and are not deprecated merely because of their age:

- `ExecutionJournal`
- ownership/account binding
- journal codec
- broker contracts
- broker reconciliation
- durable attempt/evidence primitives
- `DispatchHalted`

## Historical documentation

Documents under `docs/archive/` describe prior repository states and sprint
procedures. They remain provenance, not current development authority.

Current repository authority is:

1. `README.md`
2. `docs/STATUS.md`
3. `docs/ARCHITECTURE.md`
4. `docs/PROJECT_STRATEGY_AND_ENGINEERING_ROADMAP.md`
