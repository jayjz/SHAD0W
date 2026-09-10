# Risk model

## Authority

Risk is a future independent deterministic engine, not strategy logic. A signal can propose an `OrderIntent`; only a `RiskDecision` can authorize, modify within predeclared policy, or reject it. No strategy, broker adapter, or future LLM may bypass a risk veto. Missing, stale, inconsistent, or invalid critical state fails closed to no trade.

P0.3's `Signal` is deliberately only a proposal with reconstructable feature evidence. It has no order fields or authority to size, execute, or authorize a trade.

P0.4B's `LifecycleAction` and simulated lifecycle state are likewise not risk authorization or an order. They make the causal state consequence of already-legal strategy evidence explicit for research simulation, with no quantity, price, broker, or economic effect. A future risk component remains the independent authority to authorize or reject any execution intent in a full simulation or operational design.

P0.4C/P0.5 compose the current lifecycle, deterministic execution, and downstream economics evidence as a research-simulation baseline only. They do not assert that every P0.3 signal would pass the future risk engine: temporary admission into lifecycle and a modeled quote-side fill are explicitly not production authorization. P0.5C's fixed quantity is an experiment assumption used only after fill resolution; it is not strategy sizing, risk sizing, an authorization, or evidence of cash/liquidity capacity. Changing quantity, currency, or fees cannot alter lifecycle or execution outcomes. The runner keeps the future risk seam at signal-to-lifecycle admission and adds no risk policy, limit, or authorization mechanism.

## Future control categories

The architecture must accommodate exposure and position limits, maximum-loss constraints, catastrophic stops, stale-data vetoes, volatility controls, concurrent-position limits, session constraints, and portfolio reconciliation. These are architectural categories, not P0.0 parameter choices.

Production limits, percentages, loss budgets, and escalation policies require empirical evidence and explicit owner decisions. They must be versioned and captured in experiment or operational evidence when introduced.
