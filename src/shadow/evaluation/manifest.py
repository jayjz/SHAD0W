"""Explicit experiment identity, without ambient Git, I/O, or provenance claims."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum

from shadow.data.fingerprint import quote_evidence_fingerprint
from shadow.evaluation.models import EVALUATION_MODEL_ID, EvaluationError
from shadow.simulation.events import EventKind
from shadow.simulation.runner import SimulationResult

LIMITATIONS = (
    "Independent fixed-quantity trades; no funded portfolio, buying power, or compounding.",
    "Synthetic fees/slippage; no depth, capacity, partial fills, impact, or broker model.",
    "Nonpositive execution prices are stress evidence excluded from ordinary metrics.",
    "Descriptive observations only; no holdout, selection correction, or evidence of edge.",
    "Caller-supplied data and code revision are identified, not authenticated.",
    "Quote evidence has no dataset-level provenance metadata in SimulationResult.",
    "Opportunity identity covers supplied events, not an unavailable generator implementation.",
    "P0.1 serialization is now context-independent; older high-precision bar hashes may differ.",
)


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _canonical(value: object) -> object:
    """Encode the closed immutable domain vocabulary, not repr or arbitrary objects.

    Evidence tuples are multisets: sort encodings, retain multiplicity. Decimal
    representations are retained conservatively because execution's quote-reference
    tie-break can depend on them. No arithmetic or locale dependence is involved.
    """
    if isinstance(value, Enum):
        return {"enum": type(value).__qualname__, "value": value.value}
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return {"decimal": value.as_tuple()}
    if isinstance(value, datetime):
        return {"utc": value.astimezone(UTC).isoformat(timespec="microseconds")}
    if isinstance(value, timedelta):
        return {"microseconds": (value.days * 86400 + value.seconds) * 1000000 + value.microseconds}
    if isinstance(value, tuple):
        return sorted((_canonical(item) for item in value), key=_encode)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "contract": type(value).__module__ + "." + type(value).__qualname__,
            "fields": {
                field.name: _canonical(getattr(value, field.name)) for field in fields(value)
            },
        }
    raise EvaluationError(f"unsupported experiment identity value: {type(value).__name__}")


def fingerprint(value: object) -> str:
    return hashlib.sha256(_encode(_canonical(value)).encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    """References to exact supplied run evidence; no automatic revision discovery.

    code_revision is the caller's revision/artifact identity responsible for both
    simulation and evaluation. The caller must identify a dirty build explicitly;
    it must never silently describe that build as its clean base commit.
    """

    bar_dataset_fingerprint: str
    quote_evidence_fingerprint: str
    opportunity_evidence_fingerprint: str
    strategy_configuration_fingerprint: str
    execution_configuration_fingerprint: str
    economics_configuration_fingerprint: str
    simulation_evidence_fingerprint: str
    feature_implementation_version: str
    simulation_implementation_version: str
    code_revision: str
    simulation_id: str | None
    experiment_id: str | None
    limitations: tuple[str, ...]
    evaluation_model_id: str = EVALUATION_MODEL_ID

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name.endswith("fingerprint"):
                if (
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(c not in "0123456789abcdef" for c in value)
                ):
                    raise EvaluationError(f"{field.name} must be a SHA-256 hexadecimal digest")
            elif field.name != "limitations":
                if value is None and field.name in {"simulation_id", "experiment_id"}:
                    continue
                if not isinstance(value, str) or not value or value != value.strip():
                    raise EvaluationError(f"{field.name} must be a nonempty trimmed string")
        if self.evaluation_model_id != EVALUATION_MODEL_ID:
            raise EvaluationError("unsupported evaluation model")
        if (
            not isinstance(self.limitations, tuple)
            or any(
                not isinstance(value, str) or not value or value != value.strip()
                for value in self.limitations
            )
            or not set(LIMITATIONS).issubset(self.limitations)
        ):
            raise EvaluationError("manifest must retain mandatory and valid declared limitations")

    @property
    def identity(self) -> str:
        return fingerprint(self)


def build_manifest(
    result: SimulationResult,
    *,
    code_revision: str,
    experiment_id: str | None = None,
    limitations: tuple[str, ...] = (),
) -> ExperimentManifest:
    """Bind the evaluator to all material supplied evidence, including unused quotes/events."""
    if not isinstance(limitations, tuple):
        raise EvaluationError("limitations must be an immutable tuple")
    return ExperimentManifest(
        bar_dataset_fingerprint=result.dataset_fingerprint,
        quote_evidence_fingerprint=quote_evidence_fingerprint(result.quotes),
        opportunity_evidence_fingerprint=fingerprint(
            tuple(
                event
                for event in result.timeline.ordered_events
                if event.kind is EventKind.EXECUTION_OPPORTUNITY
            )
        ),
        strategy_configuration_fingerprint=fingerprint(result.strategy_configs),
        execution_configuration_fingerprint=fingerprint(result.execution_config),
        economics_configuration_fingerprint=fingerprint(result.economics_configs),
        simulation_evidence_fingerprint=fingerprint(result),
        feature_implementation_version=result.feature_implementation_version,
        simulation_implementation_version=result.simulation_implementation_version,
        code_revision=code_revision,
        simulation_id=result.simulation_id,
        experiment_id=experiment_id,
        limitations=(*LIMITATIONS, *limitations),
    )
