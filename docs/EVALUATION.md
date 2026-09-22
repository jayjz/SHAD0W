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
| Per-trade gross/net result and return | Implemented in P1A for ordinary completed trades only |
| Portfolio/performance evaluation | Not implemented |

P0.5C fees use the final slipped execution price times fixed quantity, with no extra spread or slippage charge. The resulting BUY/SELL execution cash flow describes one fill only; it is not cash, P&L, settlement, or portfolio state. Quote currency is caller-declared, with no FX or minor-unit rounding. Zero/negative execution-price results remain explicit stress-domain evidence and do not establish ordinary feasibility.

P1A adds a read-only evaluation boundary over a supplied `SimulationResult` with P0.5C economics enabled. It reconciles every lifecycle event, execution attempt/outcome, filled economic execution, final lifecycle state, unresolved action, and open position before pairing one authoritative long entry with its authoritative exit. Missing, duplicate, stale, future, mismatched, or causally inconsistent evidence fails closed. End-of-stream pending/open state remains incomplete evidence; it is never treated as a close.

Each result has an immutable experiment manifest. It identifies the validated bar dataset plus supplied quote and opportunity evidence, strategy/execution/economics configurations, feature/simulation implementation versions, the full supplied simulation evidence, caller-supplied code revision, and mandatory limitations. Identity is deterministic but does not authenticate caller-supplied evidence or revision. Completed trades expose gross result, total fee, net result, and—only when both execution prices are positive—gross/net return. Any zero or negative execution price is explicit stress evidence, excluded from ordinary return and currency aggregate metrics. Ordinary aggregates are separated by caller-declared quote currency and report only descriptive sums and win/loss/flat counts.

P1A is not portfolio or performance evaluation. It adds no cash balance, settlement, funded buying power, compounding, partial closes, FX conversion, minor-unit rounding, drawdown, Sharpe, benchmark, holdout, walk-forward, selection correction, parameter stability, or evidence of edge. Without quote depth, participation, depth consumption, partial fills, or impact, the model also cannot claim complete market microstructure realism.

## P1B sealed temporal study evidence

P1B freezes validated P0.1 bar bytes under their canonical SHA-256 identity, predeclares compatible strictly chronological train/development/final partitions, and binds the full candidate set and selection protocol before final evaluation. Candidate-set identity is derived from the canonical candidate-fingerprint tuple, never supplied independently. A declared development choice and the resulting sealed-study identity consume only matching P1A manifests. A final release accepts only the sealed final dataset, selected exact strategy fingerprint, execution/economics assumptions, revision, implementation identities, and declared currency. It is persisted once with exclusive non-overwriting creation and is revalidated on load, including the frozen dataset bytes and recomputed disposition.

P1B proves only that the chosen configuration was predeclared and bound to matching development evidence; it does not independently execute or verify `selection_rule`, `selection_metric`, or `selection_tie_break`. The criterion is deliberately descriptive: below the declared ordinary-trade minimum is `insufficient_evidence`; sufficient observations with aggregate selected-currency net result at or below zero is `descriptive_failure`; otherwise it is `descriptive_survival`. None is a null-hypothesis test or evidence of edge, significance, robustness, benchmark outperformance, parameter stability, risk-adjusted performance, or live readiness. P1B neither provides walk-forward replication, portfolio accounting, multiple-testing/search-adjusted inference, nor proof that no undeclared experiments occurred.
