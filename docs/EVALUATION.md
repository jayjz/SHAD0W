# Evaluation

The future evaluator will report descriptive evidence, not a verdict of profitability. At minimum it should categorize trades, exposure, turnover, gross and net performance, expectancy, average win/loss, drawdown, profit factor, volatility or risk-adjusted metrics where statistically meaningful, tail behavior, cost contribution, and regime-conditioned results.

Every result must link to its experiment manifest, dataset identity, configuration, implementation revision, and cost/execution assumptions. Results should distinguish gross from net outcomes and disclose assumptions that affect execution feasibility.

Metrics can be misleading with small samples, non-independent trades, rare regimes, tail concentration, parameter selection, or changing market conditions. They are inputs to validation alongside temporal holdouts, walk-forward analysis, benchmarks, ablations, stability analysis, and documented limitations.

P0.3/P0.4A/P0.4B supply unvalidated signal, chronological-eligibility, and price-free lifecycle evidence only. P0.4B may expose a simulated open/closed position state, but it has no quantity, fill, price, cost, return, cash, or performance result to evaluate. Its structured lifecycle trace can reconstruct a state transition or non-action, but it is not a trade record. A future evaluator must not infer profitability from its presence.

P0.4C packages that evidence into a deterministic `SimulationResult` with validated dataset identity, configured feature/strategy evidence, chronology, eligibility, and final lifecycle state. It remains unevaluable in economic terms: its neutral execution opportunities are not fills, its lifecycle admission is not risk authorization, and it supplies none of the fields needed for performance metrics. P0.5 must make cost and fill assumptions explicit before P1 can evaluate results.
