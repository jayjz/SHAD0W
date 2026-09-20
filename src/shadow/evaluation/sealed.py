"""P1B sealed temporal-study contracts and one-shot descriptive releases."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

from shadow.data import FrozenBarDatasetRef, load_frozen_bars
from shadow.evaluation.historical import HistoricalEvaluationResult
from shadow.evaluation.manifest import ExperimentManifest, fingerprint
from shadow.evaluation.models import EVALUATION_MODEL_ID, EvaluationError
from shadow.execution import EXECUTION_MODEL_ID
from shadow.strategies.models import MEAN_REVERSION_STRATEGY_VERSION

SEALED_STUDY_SCHEMA_VERSION = "shadow.sealed-study.v1"


class DescriptiveDisposition(StrEnum):
    """A descriptive holdout observation, explicitly not statistical inference."""

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    DESCRIPTIVE_FAILURE = "descriptive_failure"
    DESCRIPTIVE_SURVIVAL = "descriptive_survival"


class SealedStudyError(EvaluationError):
    """A temporal-study declaration, release, or artifact violates its firewall."""


def _nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SealedStudyError(f"{name} must be a nonempty trimmed string")


def _sha(value: str | None, name: str) -> None:
    if value is None:
        return
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SealedStudyError(f"{name} must be a SHA-256 hexadecimal digest")


def _compatible(left: FrozenBarDatasetRef, right: FrozenBarDatasetRef) -> bool:
    return (
        left.instrument_universe == right.instrument_universe
        and left.source == right.source
        and left.bar_interval_microseconds == right.bar_interval_microseconds
        and left.retrieval_method == right.retrieval_method
        and left.source_timezone == right.source_timezone
        and left.session == right.session
        and left.adjustment_policy == right.adjustment_policy
        and left.schema_version == right.schema_version
    )


@dataclass(frozen=True, slots=True)
class StudyPlan:
    """The immutable pre-final protocol; this object has no final-result input.

    ``selection_rule``, ``selection_metric``, and ``selection_tie_break`` are
    predeclared, identity-bound claims.  P1B does not execute or independently
    verify that selection procedure; ``DevelopmentSelection`` only binds a
    declared candidate to matching development evidence.
    """

    study_id: str
    hypothesis: str
    limitations: tuple[str, ...]
    train_dataset: FrozenBarDatasetRef
    development_dataset: FrozenBarDatasetRef
    final_dataset: FrozenBarDatasetRef
    candidate_fingerprints: tuple[str, ...]
    selection_rule: str
    selection_metric: str
    selection_tie_break: str
    execution_config_fingerprint: str
    economics_config_fingerprint: str | None
    quote_currency: str
    minimum_ordinary_trade_count: int
    code_revision: str
    feature_implementation_version: str
    strategy_implementation_version: str
    simulation_implementation_version: str
    execution_implementation_version: str
    evaluation_implementation_version: str
    supersedes_study_id: str | None = None
    supersession_reason: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.study_id, "study_id"),
            (self.hypothesis, "hypothesis"),
            (self.selection_rule, "selection_rule"),
            (self.selection_metric, "selection_metric"),
            (self.selection_tie_break, "selection_tie_break"),
            (self.quote_currency, "quote_currency"),
            (self.code_revision, "code_revision"),
            (self.feature_implementation_version, "feature_implementation_version"),
            (self.strategy_implementation_version, "strategy_implementation_version"),
            (self.simulation_implementation_version, "simulation_implementation_version"),
            (self.execution_implementation_version, "execution_implementation_version"),
            (self.evaluation_implementation_version, "evaluation_implementation_version"),
        ):
            _nonempty(value, name)
        _sha(self.execution_config_fingerprint, "execution_config_fingerprint")
        _sha(self.economics_config_fingerprint, "economics_config_fingerprint")
        if (
            not isinstance(self.candidate_fingerprints, tuple)
            or not self.candidate_fingerprints
            or tuple(sorted(self.candidate_fingerprints)) != self.candidate_fingerprints
            or len(set(self.candidate_fingerprints)) != len(self.candidate_fingerprints)
        ):
            raise SealedStudyError("candidate fingerprints must be a sorted unique nonempty tuple")
        for candidate in self.candidate_fingerprints:
            _sha(candidate, "candidate fingerprint")
        if not isinstance(self.limitations, tuple) or any(
            not isinstance(value, str) or not value or value != value.strip()
            for value in self.limitations
        ):
            raise SealedStudyError("limitations must be an immutable tuple of nonempty strings")
        if (
            isinstance(self.minimum_ordinary_trade_count, bool)
            or not isinstance(self.minimum_ordinary_trade_count, int)
            or self.minimum_ordinary_trade_count < 0
        ):
            raise SealedStudyError("minimum ordinary trade count must be a nonnegative integer")
        datasets = (self.train_dataset, self.development_dataset, self.final_dataset)
        if not all(isinstance(value, FrozenBarDatasetRef) for value in datasets):
            raise SealedStudyError("study partitions must be frozen bar references")
        if len({value.sha256 for value in datasets}) != 3:
            raise SealedStudyError("a dataset cannot be reused across temporal partitions")
        if not all(_compatible(datasets[0], value) for value in datasets[1:]):
            raise SealedStudyError("study partitions have incompatible experimental universes")
        if not (
            self.train_dataset.coverage_end < self.development_dataset.coverage_start
            and self.development_dataset.coverage_end < self.final_dataset.coverage_start
        ):
            raise SealedStudyError("study partitions must be strictly chronological and disjoint")
        if (self.supersedes_study_id is None) != (self.supersession_reason is None):
            raise SealedStudyError("supersession id and reason must be supplied together")
        if self.supersedes_study_id is not None:
            _sha(self.supersedes_study_id, "supersedes_study_id")
            assert self.supersession_reason is not None
            _nonempty(self.supersession_reason, "supersession_reason")

    @property
    def candidate_count(self) -> int:
        return len(self.candidate_fingerprints)

    @property
    def candidate_set_id(self) -> str:
        """The content identity of the canonical candidate-fingerprint tuple."""
        return fingerprint(self.candidate_fingerprints)

    @property
    def identity(self) -> str:
        return fingerprint(self)


def _validate_manifest(
    plan: StudyPlan, manifest: ExperimentManifest, dataset: FrozenBarDatasetRef
) -> None:
    expected_economics = plan.economics_config_fingerprint or fingerprint(None)
    if (
        manifest.bar_dataset_fingerprint != dataset.sha256
        or manifest.strategy_configuration_fingerprint not in plan.candidate_fingerprints
        or manifest.execution_configuration_fingerprint != plan.execution_config_fingerprint
        or manifest.economics_configuration_fingerprint != expected_economics
        or manifest.code_revision != plan.code_revision
        or manifest.feature_implementation_version != plan.feature_implementation_version
        or manifest.simulation_implementation_version != plan.simulation_implementation_version
        or manifest.evaluation_model_id != plan.evaluation_implementation_version
    ):
        raise SealedStudyError("P1A manifest does not correspond exactly to the sealed study plan")
    # Current P1A has no independent manifest fields for these versions.  Refuse a
    # plan that claims a different implementation than the presently executable one.
    if (
        plan.strategy_implementation_version != MEAN_REVERSION_STRATEGY_VERSION
        or plan.execution_implementation_version != EXECUTION_MODEL_ID
        or plan.evaluation_implementation_version != EVALUATION_MODEL_ID
    ):
        raise SealedStudyError("P1A cannot prove the declared implementation versions")


@dataclass(frozen=True, slots=True)
class DevelopmentSelection:
    """A declared choice bound to exactly one matching development evaluation.

    This records selection evidence but deliberately does not implement an
    optimizer or prove that the plan's declared selection procedure was run.
    """

    study_id: str
    selected_candidate_fingerprint: str
    development_manifest: ExperimentManifest
    selection_evidence: str

    def __post_init__(self) -> None:
        _nonempty(self.study_id, "study_id")
        _sha(self.selected_candidate_fingerprint, "selected_candidate_fingerprint")
        _nonempty(self.selection_evidence, "selection_evidence")
        if not isinstance(self.development_manifest, ExperimentManifest):
            raise SealedStudyError("development selection requires a complete P1A manifest")

    @property
    def development_manifest_id(self) -> str:
        return self.development_manifest.identity

    @property
    def identity(self) -> str:
        return fingerprint(self)

    @classmethod
    def from_evaluation(
        cls,
        plan: StudyPlan,
        selected_candidate_fingerprint: str,
        evaluation: HistoricalEvaluationResult,
        *,
        selection_evidence: str,
    ) -> DevelopmentSelection:
        if selected_candidate_fingerprint not in plan.candidate_fingerprints:
            raise SealedStudyError("selected configuration was not declared in the candidate set")
        _validate_manifest(plan, evaluation.manifest, plan.development_dataset)
        if evaluation.manifest.strategy_configuration_fingerprint != selected_candidate_fingerprint:
            raise SealedStudyError(
                "development manifest does not contain the selected configuration"
            )
        return cls(
            plan.study_id, selected_candidate_fingerprint, evaluation.manifest, selection_evidence
        )


@dataclass(frozen=True, slots=True)
class SealedStudy:
    """A deterministic identity over a plan and its development-only selection."""

    study_plan: StudyPlan
    development_selection: DevelopmentSelection

    def __post_init__(self) -> None:
        if self.development_selection.study_id != self.study_plan.study_id:
            raise SealedStudyError("development selection belongs to another study plan")
        if (
            self.development_selection.selected_candidate_fingerprint
            not in self.study_plan.candidate_fingerprints
        ):
            raise SealedStudyError("sealed selected candidate was not declared")
        _validate_manifest(
            self.study_plan,
            self.development_selection.development_manifest,
            self.study_plan.development_dataset,
        )
        if (
            self.development_selection.development_manifest.strategy_configuration_fingerprint
            != self.development_selection.selected_candidate_fingerprint
        ):
            raise SealedStudyError("sealed selection and development manifest disagree")

    @property
    def sealed_study_id(self) -> str:
        return fingerprint((self.study_plan, self.development_selection))


def seal_study(plan: StudyPlan, selection: DevelopmentSelection) -> SealedStudy:
    """Seal a validated declared development choice; final evaluation is absent.

    This validates candidate membership and matching development evidence, not
    execution of the declared rule, metric, or tie-break.
    """
    return SealedStudy(plan, selection)


def descriptive_disposition(
    ordinary_trade_count: int, aggregate_net_result: Decimal, minimum_trade_count: int
) -> DescriptiveDisposition:
    """Apply the sole P1B descriptive criterion; this is not a significance test."""
    if ordinary_trade_count < minimum_trade_count:
        return DescriptiveDisposition.INSUFFICIENT_EVIDENCE
    if aggregate_net_result <= 0:
        return DescriptiveDisposition.DESCRIPTIVE_FAILURE
    return DescriptiveDisposition.DESCRIPTIVE_SURVIVAL


def _summary(evaluation: HistoricalEvaluationResult, currency: str) -> tuple[int, Decimal]:
    summaries = [item for item in evaluation.currency_summaries if item.quote_currency == currency]
    if len(summaries) > 1:
        raise SealedStudyError("P1A result has duplicate currency summaries")
    if not summaries:
        return 0, Decimal(0)
    return summaries[0].eligible_trade_count, summaries[0].net_result_total


@dataclass(frozen=True, slots=True)
class FinalRelease:
    """One sealed final observation with only descriptive, currency-local meaning."""

    sealed_study: SealedStudy
    final_manifest: ExperimentManifest
    ordinary_trade_count: int
    aggregate_net_result: Decimal
    disposition: DescriptiveDisposition
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.sealed_study, SealedStudy):
            raise SealedStudyError("final release requires a sealed study")
        _validate_manifest(
            self.sealed_study.study_plan,
            self.final_manifest,
            self.sealed_study.study_plan.final_dataset,
        )
        if (
            self.final_manifest.strategy_configuration_fingerprint
            != self.sealed_study.development_selection.selected_candidate_fingerprint
        ):
            raise SealedStudyError("final manifest does not use the sealed selected candidate")
        if self.ordinary_trade_count < 0 or not self.aggregate_net_result.is_finite():
            raise SealedStudyError("invalid final descriptive summary")
        if not isinstance(self.disposition, DescriptiveDisposition):
            raise SealedStudyError("invalid final descriptive disposition")
        if self.disposition is not descriptive_disposition(
            self.ordinary_trade_count,
            self.aggregate_net_result,
            self.sealed_study.study_plan.minimum_ordinary_trade_count,
        ):
            raise SealedStudyError(
                "stored final disposition disagrees with the descriptive criterion"
            )
        if not isinstance(self.limitations, tuple) or any(
            not value or value != value.strip() for value in self.limitations
        ):
            raise SealedStudyError(
                "final limitations must be an immutable tuple of nonempty strings"
            )

    @property
    def sealed_study_id(self) -> str:
        return self.sealed_study.sealed_study_id

    @classmethod
    def from_evaluation(
        cls, sealed_study: SealedStudy, evaluation: HistoricalEvaluationResult
    ) -> FinalRelease:
        _validate_manifest(
            sealed_study.study_plan, evaluation.manifest, sealed_study.study_plan.final_dataset
        )
        count, net = _summary(evaluation, sealed_study.study_plan.quote_currency)
        disposition = descriptive_disposition(
            count, net, sealed_study.study_plan.minimum_ordinary_trade_count
        )
        limitations = (
            *sealed_study.study_plan.limitations,
            (
                "Descriptive survival is not evidence of edge, significance, "
                "robustness, or live readiness."
            ),
        )
        return cls(sealed_study, evaluation.manifest, count, net, disposition, limitations)


def _ref_json(value: FrozenBarDatasetRef) -> dict[str, Any]:
    return {
        "dataset_id": value.dataset_id,
        "sha256": value.sha256,
        "instrument_universe": list(value.instrument_universe),
        "source": value.source,
        "bar_interval_microseconds": value.bar_interval_microseconds,
        "coverage_start": value.coverage_start.isoformat(),
        "coverage_end": value.coverage_end.isoformat(),
        "record_count": value.record_count,
        "provenance_id": value.provenance_id,
        "retrieval_method": value.retrieval_method,
        "source_timezone": value.source_timezone,
        "session": value.session,
        "adjustment_policy": value.adjustment_policy,
        "schema_version": value.schema_version,
    }


def _manifest_json(value: ExperimentManifest) -> dict[str, Any]:
    document = {name: getattr(value, name) for name in value.__dataclass_fields__}
    document["limitations"] = list(value.limitations)
    return document


def _plan_json(value: StudyPlan) -> dict[str, Any]:
    document = {
        **{
            name: getattr(value, name)
            for name in value.__dataclass_fields__
            if name not in {"train_dataset", "development_dataset", "final_dataset"}
        },
        "train_dataset": _ref_json(value.train_dataset),
        "development_dataset": _ref_json(value.development_dataset),
        "final_dataset": _ref_json(value.final_dataset),
    }
    document["limitations"] = list(value.limitations)
    document["candidate_fingerprints"] = list(value.candidate_fingerprints)
    document["candidate_set_id"] = value.candidate_set_id
    return document


def _release_document(release: FinalRelease) -> dict[str, Any]:
    return {
        "schema_version": SEALED_STUDY_SCHEMA_VERSION,
        "sealed_study_id": release.sealed_study_id,
        "study_plan": _plan_json(release.sealed_study.study_plan),
        "development_selection": {
            "study_id": release.sealed_study.development_selection.study_id,
            "selected_candidate_fingerprint": (
                release.sealed_study.development_selection.selected_candidate_fingerprint
            ),
            "development_manifest": _manifest_json(
                release.sealed_study.development_selection.development_manifest
            ),
            "selection_evidence": release.sealed_study.development_selection.selection_evidence,
        },
        "dataset_references": {
            "train": _ref_json(release.sealed_study.study_plan.train_dataset),
            "development": _ref_json(release.sealed_study.study_plan.development_dataset),
            "final": _ref_json(release.sealed_study.study_plan.final_dataset),
        },
        "candidate_set": list(release.sealed_study.study_plan.candidate_fingerprints),
        "selected_candidate": (
            release.sealed_study.development_selection.selected_candidate_fingerprint
        ),
        "final_manifest": _manifest_json(release.final_manifest),
        "criterion": {
            "minimum_ordinary_trade_count": (
                release.sealed_study.study_plan.minimum_ordinary_trade_count
            ),
            "currency": release.sealed_study.study_plan.quote_currency,
        },
        "disposition": release.disposition.value,
        "observed_summary": {
            "ordinary_trade_count": release.ordinary_trade_count,
            "aggregate_net_result": str(release.aggregate_net_result),
        },
        "limitations": list(release.limitations),
    }


def _release_path(root: Path, sealed_study_id: str) -> Path:
    return root / "final-releases" / sealed_study_id[:2] / f"{sealed_study_id}.json"


def persist_final_release(root: Path, release: FinalRelease) -> Path:
    """Atomically create one non-overwriting canonical release for a sealed study."""
    target = _release_path(root, release.sealed_study_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    contents = json.dumps(_release_document(release), sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )
    descriptor, temporary_name = tempfile.mkstemp(prefix=".release-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o444)
        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            raise SealedStudyError("a final release already exists for this sealed study") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _ref_from_json(value: Any) -> FrozenBarDatasetRef:
    if not isinstance(value, dict):
        raise SealedStudyError("frozen dataset reference must be an object")
    return FrozenBarDatasetRef(
        dataset_id=value["dataset_id"],
        sha256=value["sha256"],
        instrument_universe=tuple(value["instrument_universe"]),
        source=value["source"],
        bar_interval_microseconds=value["bar_interval_microseconds"],
        coverage_start=datetime.fromisoformat(value["coverage_start"]),
        coverage_end=datetime.fromisoformat(value["coverage_end"]),
        record_count=value["record_count"],
        provenance_id=value["provenance_id"],
        retrieval_method=value["retrieval_method"],
        source_timezone=value["source_timezone"],
        session=value["session"],
        adjustment_policy=value["adjustment_policy"],
        schema_version=value["schema_version"],
    )


def _manifest_from_json(value: Any) -> ExperimentManifest:
    if not isinstance(value, dict):
        raise SealedStudyError("manifest must be an object")
    return ExperimentManifest(**{**value, "limitations": tuple(value["limitations"])})


def load_final_release(root: Path, sealed_study_id: str) -> FinalRelease:
    """Independently parse and revalidate every persisted P1B firewall relation."""
    _sha(sealed_study_id, "sealed_study_id")
    target = _release_path(root, sealed_study_id)
    try:
        document = json.loads(target.read_bytes())
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != SEALED_STUDY_SCHEMA_VERSION
        ):
            raise SealedStudyError("unsupported final release schema")
        plan_value = document["study_plan"]
        if not isinstance(plan_value, dict):
            raise SealedStudyError("study plan must be an object")
        plan_arguments = {
            key: value
            for key, value in plan_value.items()
            if key
            not in {
                "train_dataset",
                "development_dataset",
                "final_dataset",
                "candidate_set_id",
            }
        }
        plan = StudyPlan(
            **{
                **plan_arguments,
                "limitations": tuple(plan_arguments["limitations"]),
                "candidate_fingerprints": tuple(plan_arguments["candidate_fingerprints"]),
            },
            train_dataset=_ref_from_json(plan_value["train_dataset"]),
            development_dataset=_ref_from_json(plan_value["development_dataset"]),
            final_dataset=_ref_from_json(plan_value["final_dataset"]),
        )
        if plan_value.get("candidate_set_id") != plan.candidate_set_id:
            raise SealedStudyError("candidate-set identity disagrees with candidate fingerprints")
        selection_value = document["development_selection"]
        selection = DevelopmentSelection(
            study_id=selection_value["study_id"],
            selected_candidate_fingerprint=selection_value["selected_candidate_fingerprint"],
            development_manifest=_manifest_from_json(selection_value["development_manifest"]),
            selection_evidence=selection_value["selection_evidence"],
        )
        sealed = SealedStudy(plan, selection)
        summary = document["observed_summary"]
        release = FinalRelease(
            sealed,
            _manifest_from_json(document["final_manifest"]),
            summary["ordinary_trade_count"],
            Decimal(summary["aggregate_net_result"]),
            DescriptiveDisposition(document["disposition"]),
            tuple(document["limitations"]),
        )
    except SealedStudyError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SealedStudyError("malformed final release artifact") from exc
    if (
        document.get("sealed_study_id") != sealed_study_id
        or release.sealed_study_id != sealed_study_id
    ):
        raise SealedStudyError("final release sealed-study identity disagreement")
    for reference in (
        release.sealed_study.study_plan.train_dataset,
        release.sealed_study.study_plan.development_dataset,
        release.sealed_study.study_plan.final_dataset,
    ):
        try:
            load_frozen_bars(root, reference)
        except ValueError as exc:
            raise SealedStudyError("final release references an invalid frozen dataset") from exc
    expected = _release_document(release)
    if document != expected:
        raise SealedStudyError(
            "final release contains inconsistent or noncanonical derived evidence"
        )
    return release
