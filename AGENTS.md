# SHAD0W operating contract

## Mission and phase

SHAD0W is a deterministic laboratory for testing whether precisely specified market hypotheses have a reproducible, executable, risk-adjusted edge after realistic costs. **P0.2B is closed by composition of P0.2A z-score evidence, P0.3 defines one unvalidated signal-only hypothesis, P0.4A/P0.4C establish chronological and lifecycle-authoritative simulation semantics, P0.5 establishes bounded execution economics, P1A establishes manifest-bound trade reconstruction and descriptive historical evidence, P1B establishes a sealed temporal research procedure, P2A establishes the minimal deterministic paper risk authority, and P4A Alpaca live-data shadow is complete.** P2A is limited to provider-neutral intents, explicit fresh state, long equity-style entry/full-exit rules, atomic single-process admission/reservation, and a one-use application dispatch capability. It adds no broker connection, external order submission, persistent idempotency, reconciliation, account/portfolio model, or live-capital authority. P4A adds market data and observational candidates only.

P5A Alpaca PAPER execution is explicitly authorized as the next engineering direction. P5A.1 provides provider-neutral broker contracts and a deterministic fake. P5A.2A provides durable identity, local ownership, and journal create/reopen metadata only; it creates no events, recovery, admission, capability, broker connection, or order path. [The execution contract](docs/P5A_EXECUTION_CONTRACT.md), [ADR 0005](docs/decisions/0005-paper-execution-recovery.md), and [bounded follow-up plan](docs/P5A_EXECUTION_PLAN.md) define the remaining work. Broker execution remains unimplemented. No trading/account endpoint connection, order submission, trading credentials, Alpaca SDK, or new HTTP dependency is permitted in this slice. Follow-up implementation and an operator-approved canary run must satisfy the contract before any paper dispatch. Live-capital trading remains prohibited.

## Source of truth

1. Executable tests and verified behavior
2. This file
3. Explicit contracts
4. `docs/ARCHITECTURE.md` and `docs/RESEARCH_METHOD.md`
5. The roadmap
6. The README
7. Comments and historical notes

## Non-negotiable invariants

- Same data, configuration, implementation, and seed must reproduce results unless documented otherwise.
- A decision may use only information available at its modeled time. Fail closed on missing, stale, invalid, or inconsistent critical state.
- Signals propose; the independent risk engine authorizes or rejects; execution obeys; broker reconciliation establishes operational truth. No strategy may bypass risk.
- Costs and execution feasibility belong in evaluation. A profitable backtest is evidence requiring validation, not proof of a durable edge.
- Core research must not depend on a broker, provider SDK, or LLM. LLMs may classify unstructured inputs later, but never hold trading or risk authority.
- Structured evidence—not conversation history—must eventually reconstruct material decisions and experiments.

## Working expectations

- Keep a modular monolith; add abstractions only when a concrete boundary requires one.
- Use typed, provider-neutral domain contracts at boundaries. Do not let Pandas, HTTP, broker, or LLM response objects escape their adapters.
- Document and test behavior with the change. As each invariant becomes executable, add focused regression or property tests.
- Experiments require immutable data identity, configuration, code revision, seed where applicable, and recorded limitations. Never silently change strategy logic, parameters, or evaluation configuration.
- A P1B sealed study may release its final holdout exactly once, only against its predeclared frozen dataset, candidate, assumptions, implementation identity, and descriptive criterion. A changed material input is a new study, never an overwrite.
- No live-capital trading or live trading endpoint support is permitted. P5A paper integration is bounded by the explicitly assigned follow-up slice; P5A.2A adds only durable identity, local ownership, and journal metadata. Preserve the separate P4A market-data-only command.
- Distinguish environment/dependency failures from product failures in reports and commits.

## Git and verification

- Preserve user work; inspect status before broad changes. Do not reset, clean, force-push, rewrite history, or push without explicit authorization.
- Use small coherent commits only after reviewing the full diff.
- Standard checks (once dependencies are installed) are `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests`, and `git diff --check`.
- Update the relevant contract, ADR, roadmap, or research documentation whenever a change alters an architectural or scientific claim.
