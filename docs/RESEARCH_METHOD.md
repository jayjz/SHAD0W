# Research method

## What counts as evidence

Each strategy begins as an explicit, falsifiable hypothesis: its universe, signal, holding/exit logic, decision time, parameters, benchmark, and cost/execution assumptions are recorded before evaluation. A profitable backtest is evidence requiring validation, not proof of a durable market edge.

Evidence should be reproducible from immutable dataset identity and provenance, configuration, code revision, seed where applicable, and structured outputs. Failed experiments, negative results, and known limitations are evidence too; they must not be silently discarded.

## Temporal discipline

Use chronological train/development/validation/final-holdout partitions appropriate to the hypothesis. Final holdout data is not used to choose parameters. Walk-forward evaluation should repeat the chronological process across successive windows when enough data exists.

Prevent look-ahead bias by modeling when each datum became available. P0.1 represents completed bars at interval end and records a separate UTC availability instant; later decision/simulation code must consume only records whose availability instant has arrived. Completed-bar strategies cannot assume knowledge of that bar's future path to obtain a same-bar fill. Address survivorship bias when the instrument universe can change over time. Preserve source timezone and session semantics as provenance throughout processing.

## Falsification and robustness

Treat parameter mining and multiple testing as threats to evidence: record alternatives considered and avoid presenting the best discovered configuration as an independent result. Evaluate explicit transaction costs, sensitivity to plausible slippage, latency, and execution constraints. Compare against relevant benchmarks, inspect parameter stability, and use ablations to identify which assumptions or components create the result.

Report sample size and uncertainty honestly. Small samples, rare regimes, and concentrated outcomes can make conventional-looking metrics misleading. Research claims must state the limitations of the data and model rather than imply certainty. Temporal correctness, deterministic behavior, and risk authority are documented requirements now and must receive executable regression or property tests when their implementations are introduced.
