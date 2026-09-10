"""First descriptive historical results, strictly without portfolio interpretation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext

from shadow.evaluation.manifest import ExperimentManifest, build_manifest
from shadow.evaluation.models import EvaluationError, TradeEligibility, TradeRecord, numeric_context
from shadow.evaluation.trades import TradeReconstructionResult, reconstruct_trades
from shadow.simulation.runner import SimulationResult


@dataclass(frozen=True, slots=True)
class CurrencyTradeSummary:
    """Sums of ordinary independently modeled trade economics in exactly one currency.

    Zero sums with zero eligible trades mean no observations, not zero performance.
    Each sum uses the canonical completed-trade order at fixed Decimal precision.
    """

    quote_currency: str
    trades: tuple[TradeRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.trades, tuple) or any(
            trade.quote_currency != self.quote_currency
            or trade.eligibility is not TradeEligibility.ORDINARY
            for trade in self.trades
        ):
            raise EvaluationError("currency summary requires ordinary same-currency trades")
        if len({trade.entry_action_reference for trade in self.trades}) != len(self.trades) or len(
            {trade.exit_action_reference for trade in self.trades}
        ) != len(self.trades):
            raise EvaluationError("duplicate summary trade")
        object.__setattr__(
            self,
            "trades",
            tuple(
                sorted(
                    self.trades,
                    key=lambda t: (t.exit_time, t.instrument.identifier, t.exit_action_reference),
                )
            ),
        )
        self._totals()

    def _totals(self) -> tuple[Decimal, Decimal, Decimal]:
        try:
            with localcontext(numeric_context()):
                return (
                    sum((t.gross_result for t in self.trades), Decimal(0)),
                    sum((t.total_fees for t in self.trades), Decimal(0)),
                    sum((t.net_result for t in self.trades), Decimal(0)),
                )
        except DecimalException as exc:
            raise EvaluationError("aggregate exceeds the Decimal numeric domain") from exc

    @property
    def eligible_trade_count(self) -> int:
        return len(self.trades)

    @property
    def gross_result_total(self) -> Decimal:
        return self._totals()[0]

    @property
    def total_fees(self) -> Decimal:
        return self._totals()[1]

    @property
    def net_result_total(self) -> Decimal:
        return self._totals()[2]

    @property
    def winning_trade_count(self) -> int:
        return sum(t.net_result > 0 for t in self.trades)

    @property
    def losing_trade_count(self) -> int:
        return sum(t.net_result < 0 for t in self.trades)

    @property
    def flat_trade_count(self) -> int:
        return sum(t.net_result == 0 for t in self.trades)


@dataclass(frozen=True, slots=True)
class HistoricalEvaluationResult:
    manifest: ExperimentManifest
    reconstruction: TradeReconstructionResult

    @property
    def completed_trade_count(self) -> int:
        return len(self.reconstruction.completed_trades)

    @property
    def eligible_trade_count(self) -> int:
        return sum(
            t.eligibility is TradeEligibility.ORDINARY for t in self.reconstruction.completed_trades
        )

    @property
    def stress_trade_count(self) -> int:
        return self.completed_trade_count - self.eligible_trade_count

    @property
    def incomplete_trade_count(self) -> int:
        return self.reconstruction.incomplete_trade_count

    @property
    def currency_summaries(self) -> tuple[CurrencyTradeSummary, ...]:
        trades = self.reconstruction.completed_trades
        return tuple(
            CurrencyTradeSummary(
                currency,
                tuple(
                    t
                    for t in trades
                    if t.quote_currency == currency and t.eligibility is TradeEligibility.ORDINARY
                ),
            )
            for currency in sorted({t.quote_currency for t in trades})
        )


def evaluate_historical(
    result: SimulationResult,
    *,
    code_revision: str,
    experiment_id: str | None = None,
    limitations: tuple[str, ...] = (),
) -> HistoricalEvaluationResult:
    """Observe one explicitly configured historical simulation; no selection or feedback."""
    reconstruction = reconstruct_trades(result)
    manifest = build_manifest(
        result, code_revision=code_revision, experiment_id=experiment_id, limitations=limitations
    )
    evaluation = HistoricalEvaluationResult(manifest, reconstruction)
    _ = evaluation.currency_summaries  # Fail numeric overflow before returning aggregate results.
    return evaluation
