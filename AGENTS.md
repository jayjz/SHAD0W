# SHAD0W operating contract

## Mission and phase

SHAD0W is a deterministic laboratory for testing whether precisely specified market hypotheses have a reproducible, executable, risk-adjusted edge after realistic costs. **P0.2B is closed by composition of P0.2A z-score evidence, P0.3 defines one unvalidated signal-only hypothesis, P0.4A/P0.4C establish chronological and lifecycle-authoritative simulation semantics, P0.5A establishes explicit deterministic quote-side execution outcomes, P0.5B adds deterministic adverse slippage and price-level sensitivity evidence, P0.5C attaches fixed declared quantity and proportional fee evidence to filled outcomes, and P1A reconstructs completed economic executions into deterministic trades with manifest-bound descriptive historical evidence.** P0.5 remains bounded execution evidence rather than complete market microstructure realism. P1A does not authorize portfolio evaluation, validation/holdout design, walk-forward analysis, or a further milestone.

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
- Signals propose; the independent risk engine authorizes or rejects. No strategy may bypass risk.
- Costs and execution feasibility belong in evaluation. A profitable backtest is evidence requiring validation, not proof of a durable edge.
- Core research must not depend on a broker, provider SDK, or LLM. LLMs may classify unstructured inputs later, but never hold trading or risk authority.
- Structured evidence—not conversation history—must eventually reconstruct material decisions and experiments.

## Working expectations

- Keep a modular monolith; add abstractions only when a concrete boundary requires one.
- Use typed, provider-neutral domain contracts at boundaries. Do not let Pandas, HTTP, broker, or LLM response objects escape their adapters.
- Document and test behavior with the change. As each invariant becomes executable, add focused regression or property tests.
- Experiments require immutable data identity, configuration, code revision, seed where applicable, and recorded limitations. Never silently change strategy logic, parameters, or evaluation configuration.
- No live trading, order submission, or broker integration is permitted in current milestones.
- Distinguish environment/dependency failures from product failures in reports and commits.

## Git and verification

- Preserve user work; inspect status before broad changes. Do not reset, clean, force-push, rewrite history, or push without explicit authorization.
- Use small coherent commits only after reviewing the full diff.
- Standard checks (once dependencies are installed) are `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests`, and `git diff --check`.
- Update the relevant contract, ADR, roadmap, or research documentation whenever a change alters an architectural or scientific claim.
