# SHAD0W

SHAD0W is a deterministic quantitative research laboratory. Its central question is whether a precisely specified market hypothesis produces a reproducible, executable, risk-adjusted edge after realistic costs.

P0.0 establishes the repository operating foundation only. It contains no trading strategy, market-data implementation, simulator, broker integration, order submission, or LLM integration. P0.1 will introduce deterministic market-data contracts.

## Development

The project supports Python 3.12 and newer. [`uv`](https://docs.astral.sh/uv/) is the preferred local environment manager.

```powershell
uv sync --group dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
```

The smoke test verifies that the bootstrap package can be imported; it is not evidence that research or trading behavior is correct. See [the roadmap](docs/PROJECT_STRATEGY_AND_ENGINEERING_ROADMAP.md), [architecture](docs/ARCHITECTURE.md), and [research method](docs/RESEARCH_METHOD.md).

## License

No license has been selected. External reuse and contribution rights are not granted until the project owner makes and records a licensing decision.
