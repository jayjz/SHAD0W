# Evaluation

The future evaluator will report descriptive evidence, not a verdict of profitability. At minimum it should categorize trades, exposure, turnover, gross and net performance, expectancy, average win/loss, drawdown, profit factor, volatility or risk-adjusted metrics where statistically meaningful, tail behavior, cost contribution, and regime-conditioned results.

Every result must link to its experiment manifest, dataset identity, configuration, implementation revision, and cost/execution assumptions. Results should distinguish gross from net outcomes and disclose assumptions that affect execution feasibility.

Metrics can be misleading with small samples, non-independent trades, rare regimes, tail concentration, parameter selection, or changing market conditions. They are inputs to validation alongside temporal holdouts, walk-forward analysis, benchmarks, ablations, stability analysis, and documented limitations.

P0.3/P0.4A supply unvalidated signal and chronological-eligibility evidence only; they have no trades, fills, positions, costs, or performance results to evaluate. P0.4A's structured timeline trace can reconstruct why an opportunity was eligible or rejected, but it is not a trade record. A future evaluator must not infer profitability from its presence.
