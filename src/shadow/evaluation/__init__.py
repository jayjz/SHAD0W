"""Read-only deterministic trade reconstruction and historical evaluation."""

from shadow.evaluation.historical import (
    CurrencyTradeSummary,
    HistoricalEvaluationResult,
    evaluate_historical,
)
from shadow.evaluation.manifest import ExperimentManifest, build_manifest
from shadow.evaluation.models import (
    EVALUATION_MODEL_ID,
    EvaluationError,
    TradeEligibility,
    TradeRecord,
)
from shadow.evaluation.trades import TradeReconstructionResult, reconstruct_trades

__all__ = [
    "EVALUATION_MODEL_ID",
    "CurrencyTradeSummary",
    "EvaluationError",
    "ExperimentManifest",
    "HistoricalEvaluationResult",
    "TradeEligibility",
    "TradeRecord",
    "TradeReconstructionResult",
    "build_manifest",
    "evaluate_historical",
    "reconstruct_trades",
]
