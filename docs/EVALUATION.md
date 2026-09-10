# Evaluation

The future evaluator will report descriptive evidence, not a verdict of profitability. At minimum it should categorize trades, exposure, turnover, gross and net performance, expectancy, average win/loss, drawdown, profit factor, volatility or risk-adjusted metrics where statistically meaningful, tail behavior, cost contribution, and regime-conditioned results.

Every result must link to its experiment manifest, dataset identity, configuration, implementation revision, and cost/execution assumptions. Results should distinguish gross from net outcomes and disclose assumptions that affect execution feasibility.

Metrics can be misleading with small samples, non-independent trades, rare regimes, tail concentration, parameter selection, or changing market conditions. They are inputs to validation alongside temporal holdouts, walk-forward analysis, benchmarks, ablations, stability analysis, and documented limitations.

P0.3/P0.4A/P0.4B supply unvalidated signal, chronological eligibility, and lifecycle-intent evidence only. P0.4B may expose a simulated open/closed position state, but P0.4's structural opportunity is not itself a fill, price, cost, return, cash, or performance result.

P0.4C/P0.5A package that evidence into a deterministic `SimulationResult` with validated bar identity, configured feature/strategy evidence, chronology, eligibility, final lifecycle state, and explicit quote-side execution attempts/outcomes. P0.5A's `filled` outcome is modeled executable-price evidence, not a profit claim: it has no quantity, commissions, added spread charge, slippage, cash, P&L, return, equity, or performance metric. Its lifecycle admission is not risk authorization. P0.5B must make the next explicit cost/slippage assumption before P1 can evaluate results.
