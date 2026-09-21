# SHAD0W operating contract

## Mission and phase

SHAD0W is a deterministic laboratory for testing whether precisely specified market hypotheses have a reproducible, executable, risk-adjusted edge after realistic costs. P0–P2A and P4A are implemented; P4A includes verified warm-start of historical feature context. P2A remains a bounded provider-neutral risk authority, while P4A produces FLAT observational candidates only. Current capability status belongs in [docs/STATUS.md](docs/STATUS.md), architecture boundaries in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), and P5A dependencies in [docs/P5A_EXECUTION_PLAN.md](docs/P5A_EXECUTION_PLAN.md).

HEAD includes an early bounded supervised one-shot PAPER integration probe: broker contracts, durable journal/identity, PAPER adapter, preparation, and guarded one-shot dispatch. It is not continuous PAPER trading, P5A.3 broker-authoritative reconciliation/lifecycle projection, recovery acceptance, or final P5A.8 canary acceptance. Do not start P5A.3 or expand PAPER behavior without an explicit assignment. Live-capital trading remains prohibited.

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
- No live-capital trading or live trading endpoint support is permitted. P5A paper integration is bounded by the explicitly assigned follow-up slice; P5A.2A adds only durable identity, local ownership, and journal metadata. Preserve the separate P4A market-data-only command.
- Distinguish environment/dependency failures from product failures in reports and commits.

## Git and verification

- Preserve user work; inspect status before broad changes. Do not reset, clean, force-push, rewrite history, or push without explicit authorization.
- Use small coherent commits only after reviewing the full diff.
- Standard checks (once dependencies are installed) are `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests`, and `git diff --check`.
- Update the relevant contract, ADR, roadmap, or research documentation whenever a change alters an architectural or scientific claim.
