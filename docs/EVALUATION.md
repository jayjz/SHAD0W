# Evaluation

The future evaluator will report descriptive evidence, not a verdict of profitability. At minimum it should categorize trades, exposure, turnover, gross and net performance, expectancy, average win/loss, drawdown, profit factor, volatility or risk-adjusted metrics where statistically meaningful, tail behavior, cost contribution, and regime-conditioned results.

Every result must link to its experiment manifest, dataset identity, configuration, implementation revision, and cost/execution assumptions. Results should distinguish gross from net outcomes and disclose assumptions that affect execution feasibility.

Metrics can be misleading with small samples, non-independent trades, rare regimes, tail concentration, parameter selection, or changing market conditions. They are inputs to validation alongside temporal holdouts, walk-forward analysis, benchmarks, ablations, stability analysis, and documented limitations.

P0.3/P0.4A/P0.4B supply unvalidated signal, chronological eligibility, and lifecycle-intent evidence only. P0.4B may expose a simulated open/closed position state, but P0.4's structural opportunity is not itself a fill, price, cost, return, cash, or performance result.

P0.4C/P0.5A package that evidence into a deterministic `SimulationResult` with validated bar identity, configured feature/strategy evidence, chronology, eligibility, final lifecycle state, and explicit quote-side execution attempts/outcomes. P0.5A's `filled` outcome is modeled executable-price evidence, not a profit claim: it has no quantity, commissions, added spread charge, slippage, cash, P&L, return, equity, or performance metric. Its lifecycle admission is not risk authorization. P0.5B now supplies the first explicit adverse slippage assumption, without evaluating strategy performance.

P0.5B compares caller-supplied adverse bps assumptions at the price level only: exact executable quote side, declared slippage, modeled price, status, and lifecycle evidence. BUY worsens upward from ask; SELL worsens downward from bid. The immutable sensitivity tuples are evidence about an assumption, not empirical execution calibration or profitability. Illustrative bps fixtures are not production recommendations.

| Execution component | Current status |
| --- | --- |
| Quoted spread, quote-side fills, quote freshness | Implemented in P0.5A |
| Deterministic adverse slippage and price sensitivity | Implemented in P0.5B |
| Quantity and sizing | Not implemented |
| Fees | Not implemented; absence is a limitation, not zero-cost or realistic execution evidence |
| Market impact | Not implemented |
| Partial fills | Not implemented |
| P&L and portfolio/performance evaluation | Not implemented |

Fees commonly require quantity, notional, venue, security type, and order behavior, none of which this execution model supplies. Without quantity/depth, participation, depth consumption, impact, and partial fills cannot be modeled honestly; no extra bps constant stands in for them. P0.5 remains incomplete. The smallest proposed next execution-economics slice is a separately authorized quantity/fee evidence contract, specification first, before implementing fees or evaluating net performance. No sizing or accounting implementation is authorized by this proposal.
