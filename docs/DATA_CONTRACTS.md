# Data contracts

P0.1 will implement typed Python models. This document defines the provider-neutral concepts now; no external response object is a cross-system contract.

| Contract | Purpose |
| --- | --- |
| `Bar` | OHLCV observation with instrument, interval, and availability time |
| `Quote` | Bid/ask market observation with timestamps and provenance |
| `DatasetMetadata` | Dataset identity, source, retrieval/coverage details, and quality notes |
| `FeatureSnapshot` | Time-indexed, versioned deterministic feature values |
| `Signal` | Strategy proposal, rationale fields, and decision timestamp |
| `RegimeState` | Versioned deterministic market-state classification |
| `EventRiskState` | Typed future event classification with source, timestamp, and expiry |
| `PortfolioState` | Time-indexed positions, cash, valuation inputs, and reconciliation state |
| `RiskDecision` | Authorization, rejection, or constrained approval with reasons |
| `OrderIntent` | Provider-neutral requested action after risk authorization |
| `ExecutionReport` | Modeled or external execution outcome and costs |
| `TradeRecord` | Reconstructable lifecycle record for an evaluated or executed trade |
| `ExperimentManifest` | Hypothesis, configuration, code, data identity, seed, and assumptions |
| `EvaluationReport` | Versioned descriptive results, limitations, and linked evidence |

All timestamps must be timezone-aware instants, represented in UTC at boundaries; source timezone/session context is retained in metadata where relevant. Contracts must distinguish observation time, availability time, decision time, and execution/fill time. Dataset provenance must identify source, retrieval or generation details, covered instruments and intervals, adjustments, and validation status. Invalid or incomplete critical input cannot be silently repaired into trading state.
