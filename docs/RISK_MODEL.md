# Risk model

## Authority

Risk is a future independent deterministic engine, not strategy logic. A signal can propose an `OrderIntent`; only a `RiskDecision` can authorize, modify within predeclared policy, or reject it. No strategy, broker adapter, or future LLM may bypass a risk veto. Missing, stale, inconsistent, or invalid critical state fails closed to no trade.

## Future control categories

The architecture must accommodate exposure and position limits, maximum-loss constraints, catastrophic stops, stale-data vetoes, volatility controls, concurrent-position limits, session constraints, and portfolio reconciliation. These are architectural categories, not P0.0 parameter choices.

Production limits, percentages, loss budgets, and escalation policies require empirical evidence and explicit owner decisions. They must be versioned and captured in experiment or operational evidence when introduced.
