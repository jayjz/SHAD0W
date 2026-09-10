# Evaluation

The future evaluator will report descriptive evidence, not a verdict of profitability. At minimum it should categorize trades, exposure, turnover, gross and net performance, expectancy, average win/loss, drawdown, profit factor, volatility or risk-adjusted metrics where statistically meaningful, tail behavior, cost contribution, and regime-conditioned results.

Every result must link to its experiment manifest, dataset identity, configuration, implementation revision, and cost/execution assumptions. Results should distinguish gross from net outcomes and disclose assumptions that affect execution feasibility.

Metrics can be misleading with small samples, non-independent trades, rare regimes, tail concentration, parameter selection, or changing market conditions. They are inputs to validation alongside temporal holdouts, walk-forward analysis, benchmarks, ablations, stability analysis, and documented limitations.

P0.3/P0.4A/P0.4B supply unvalidated signal, chronological eligibility, and lifecycle-intent evidence only. P0.4B may expose a simulated open/closed position state, but P0.4's structural opportunity is not itself a fill, price, cost, return, cash, or performance result.

P0.4C/P0.5 package that evidence into a deterministic `SimulationResult` with validated bar identity, configured feature/strategy evidence, chronology, eligibility, final lifecycle state, explicit quote-side execution attempts/outcomes, and optional filled-execution economics. P0.5A's `filled` outcome remains modeled executable-price evidence rather than a profit claim. P0.5B supplies explicit adverse slippage, and P0.5C supplies fixed declared quantity and a synthetic proportional fee without evaluating strategy performance.

P0.5B compares caller-supplied adverse bps assumptions at the price level only: exact executable quote side, declared slippage, modeled price, status, and lifecycle evidence. BUY worsens upward from ask; SELL worsens downward from bid. The immutable sensitivity tuples are evidence about an assumption, not empirical execution calibration or profitability. Illustrative bps fixtures are not production recommendations.

| Execution component | Current status |
| --- | --- |
| Quoted spread, quote-side fills, quote freshness | Implemented in P0.5A |
| Deterministic adverse slippage and price sensitivity | Implemented in P0.5B |
| Fixed execution quantity | Implemented in P0.5C as a caller-declared research assumption; no sizing authority |
| Fees | Implemented in P0.5C as synthetic bps of absolute final executed notional; no broker schedule claim |
| Market impact | Not implemented |
| Partial fills | Not implemented |
| P&L and portfolio/performance evaluation | Not implemented |

P0.5C fees use the final slipped execution price times fixed quantity, with no extra spread or slippage charge. The resulting BUY/SELL execution cash flow describes one fill only; it is not cash, P&L, settlement, or portfolio state. Quote currency is caller-declared, with no FX or minor-unit rounding. Zero/negative execution-price results remain explicit stress-domain evidence and do not establish ordinary feasibility.

Without quote depth, participation, depth consumption, partial fills, or impact, the model cannot claim complete market microstructure realism. P0.5 is complete only under the bounded definition of quote-side pricing, deterministic slippage, fixed quantity, and proportional fee evidence. P1A trade reconstruction and the first historical-evaluation foundation are next; complete experiment identity, gross/net trade evaluation, and performance evidence remain absent until then.
