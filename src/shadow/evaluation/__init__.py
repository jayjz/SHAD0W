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
from shadow.evaluation.sealed import (
    DescriptiveDisposition,
    DevelopmentSelection,
    FinalRelease,
    SealedStudy,
    SealedStudyError,
    StudyPlan,
    descriptive_disposition,
    load_final_release,
    persist_final_release,
    seal_study,
)
from shadow.evaluation.trades import TradeReconstructionResult, reconstruct_trades

__all__ = [
    "EVALUATION_MODEL_ID",
    "CurrencyTradeSummary",
    "DescriptiveDisposition",
    "DevelopmentSelection",
    "EvaluationError",
    "ExperimentManifest",
    "HistoricalEvaluationResult",
    "FinalRelease",
    "SealedStudy",
    "SealedStudyError",
    "StudyPlan",
    "TradeEligibility",
    "TradeRecord",
    "TradeReconstructionResult",
    "build_manifest",
    "evaluate_historical",
    "descriptive_disposition",
    "load_final_release",
    "persist_final_release",
    "reconstruct_trades",
    "seal_study",
]
