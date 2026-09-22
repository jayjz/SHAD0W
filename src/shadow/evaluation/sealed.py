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
