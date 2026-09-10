# SHAD0W

SHAD0W is a deterministic quantitative research laboratory. Its central question is whether a precisely specified market hypothesis produces a reproducible, executable, risk-adjusted edge after realistic costs.

P0.0 established the repository operating foundation. P0.1 established typed, provider-neutral market-data contracts with UTC availability semantics, deterministic validation, provenance, canonical SHA-256 dataset identity, and local synthetic fixtures. P0.2A established a small deterministic close-price feature kernel: simple returns, strict full-window population rolling statistics and z-scores, semantic warm-up/unavailability states, and availability propagation. P0.2B is complete without additional code: the initial condition is expressible by thresholding the existing z-score. P0.3 adds one deterministic, provider-neutral, signal-only mean-reversion hypothesis; it is not evidence of profitability and contains no simulation, risk authorization, execution, broker integration, or order submission. P0.4 is next.

## Development

The project supports Python 3.12 and newer. [`uv`](https://docs.astral.sh/uv/) is the preferred local environment manager.

```powershell
uv sync --group dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
```

The checks cover contract behavior, not research or trading profitability. See [data contracts](docs/DATA_CONTRACTS.md), [the roadmap](docs/PROJECT_STRATEGY_AND_ENGINEERING_ROADMAP.md), [architecture](docs/ARCHITECTURE.md), and [research method](docs/RESEARCH_METHOD.md).

## License

No license has been selected. External reuse and contribution rights are not granted until the project owner makes and records a licensing decision.
