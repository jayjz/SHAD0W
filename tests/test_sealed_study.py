"""P1B temporal-firewall, selection, sealing, and one-shot-release tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from shadow.data import freeze_bars
from shadow.evaluation import (
    DescriptiveDisposition,
    DevelopmentSelection,
    FinalRelease,
    SealedStudyError,
    StudyPlan,
    descriptive_disposition,
    load_final_release,
    persist_final_release,
    seal_study,
)
from shadow.evaluation.historical import HistoricalEvaluationResult, evaluate_historical
from shadow.evaluation.manifest import fingerprint
from shadow.execution import EXECUTION_MODEL_ID
from shadow.simulation import run_simulation
from shadow.strategies.models import MEAN_REVERSION_STRATEGY_VERSION
from tests.test_evaluation import REVISION, _run_input
from tests.test_market_validation import _bars, _metadata


def _refs(tmp_path: Path):  # type: ignore[no-untyped-def]
    bars, metadata = _bars("valid_bars.json"), _metadata()
    refs = []
    for index, name in enumerate(("train", "development", "final")):
        shift = timedelta(days=index * 3)
        shifted = [
            replace(
                item,
                observation_time=item.observation_time + shift,
                availability_time=item.availability_time + shift,
            )
            for item in bars
        ]
        shifted_metadata = replace(
            metadata,
            dataset_id=name,
            coverage_start=metadata.coverage_start + shift,
            coverage_end=metadata.coverage_end + shift,
        )
        refs.append(freeze_bars(tmp_path, shifted, shifted_metadata))
    return tuple(refs)


def _plan(
    tmp_path: Path,
) -> tuple[StudyPlan, HistoricalEvaluationResult, HistoricalEvaluationResult]:
    train, development, final = _refs(tmp_path)
    evaluation = evaluate_historical(run_simulation(_run_input()), code_revision=REVISION)
    manifest = evaluation.manifest
    candidate = manifest.strategy_configuration_fingerprint
    common = dict(
        code_revision=REVISION,
        feature_implementation_version=manifest.feature_implementation_version,
        strategy_implementation_version=MEAN_REVERSION_STRATEGY_VERSION,
        simulation_implementation_version=manifest.simulation_implementation_version,
        execution_implementation_version=EXECUTION_MODEL_ID,
        evaluation_implementation_version=manifest.evaluation_model_id,
    )
    plan = StudyPlan(
        study_id="sealed-fixture",
        hypothesis="test only",
        limitations=("fixture evidence",),
        train_dataset=train,
        development_dataset=development,
        final_dataset=final,
        candidate_fingerprints=(candidate,),
        selection_rule="highest declared net total",
        selection_metric="net result",
        selection_tie_break="fingerprint",
        execution_config_fingerprint=manifest.execution_configuration_fingerprint,
        economics_config_fingerprint=manifest.economics_configuration_fingerprint,
        quote_currency="USD",
        minimum_ordinary_trade_count=1,
        **common,
    )
    development_evaluation = replace(
        evaluation, manifest=replace(manifest, bar_dataset_fingerprint=development.sha256)
    )
    final_evaluation = replace(
        evaluation, manifest=replace(manifest, bar_dataset_fingerprint=final.sha256)
    )
    return plan, development_evaluation, final_evaluation


def test_temporal_firewall_and_selection_require_exact_declared_evidence(tmp_path: Path) -> None:
    plan, development, final = _plan(tmp_path)
    candidate = plan.candidate_fingerprints[0]
    with pytest.raises(SealedStudyError, match="reused across"):
        replace(plan, development_dataset=plan.train_dataset)
    with pytest.raises(SealedStudyError, match="not declared"):
        DevelopmentSelection.from_evaluation(plan, "0" * 64, development, selection_evidence="rule")
    with pytest.raises(SealedStudyError, match="does not correspond"):
        DevelopmentSelection.from_evaluation(plan, candidate, final, selection_evidence="rule")
    selection = DevelopmentSelection.from_evaluation(
        plan, candidate, development, selection_evidence="rule"
    )
    sealed = seal_study(plan, selection)
    assert sealed.sealed_study_id == seal_study(plan, selection).sealed_study_id


def test_candidate_set_identity_is_derived_and_persisted_disagreement_rejects(
    tmp_path: Path,
) -> None:
    plan, development, final = _plan(tmp_path)
    selection = DevelopmentSelection.from_evaluation(
        plan, plan.candidate_fingerprints[0], development, selection_evidence="rule"
    )
    release = FinalRelease.from_evaluation(seal_study(plan, selection), final)
    path = persist_final_release(tmp_path, release)

    assert plan.candidate_set_id == fingerprint(plan.candidate_fingerprints)
    path.chmod(0o644)
    path.write_bytes(path.read_bytes().replace(plan.candidate_set_id.encode(), b"0" * 64, 1))

    with pytest.raises(SealedStudyError, match="candidate-set identity"):
        load_final_release(tmp_path, release.sealed_study_id)


def test_temporal_overlap_and_reversed_partitions_reject(tmp_path: Path) -> None:
    plan, _, _ = _plan(tmp_path)
    overlapping_development = replace(
        plan.development_dataset,
        coverage_start=plan.train_dataset.coverage_start,
    )
    with pytest.raises(SealedStudyError, match="strictly chronological"):
        replace(plan, development_dataset=overlapping_development)
    with pytest.raises(SealedStudyError, match="strictly chronological"):
        replace(
            plan,
            train_dataset=plan.development_dataset,
            development_dataset=plan.train_dataset,
        )


@pytest.mark.parametrize(
    "field",
    (
        "code_revision",
        "feature_implementation_version",
        "simulation_implementation_version",
        "evaluation_implementation_version",
    ),
)
def test_development_manifest_rejects_revision_and_implementation_mismatches(
    tmp_path: Path, field: str
) -> None:
    plan, development, _ = _plan(tmp_path)
    mismatch = f"mismatched-{field}"
    if field == "code_revision":
        mismatched_plan = replace(plan, code_revision=mismatch)
    elif field == "feature_implementation_version":
        mismatched_plan = replace(plan, feature_implementation_version=mismatch)
    elif field == "simulation_implementation_version":
        mismatched_plan = replace(plan, simulation_implementation_version=mismatch)
    else:
        mismatched_plan = replace(plan, evaluation_implementation_version=mismatch)

    with pytest.raises(SealedStudyError, match="does not correspond"):
        DevelopmentSelection.from_evaluation(
            mismatched_plan,
            mismatched_plan.candidate_fingerprints[0],
            development,
            selection_evidence="declared only",
        )


@pytest.mark.parametrize("field", ("execution_config_fingerprint", "economics_config_fingerprint"))
def test_development_manifest_rejects_execution_and_economics_mismatches(
    tmp_path: Path, field: str
) -> None:
    plan, development, _ = _plan(tmp_path)
    if field == "execution_config_fingerprint":
        mismatched_plan = replace(plan, execution_config_fingerprint="0" * 64)
    else:
        mismatched_plan = replace(plan, economics_config_fingerprint="0" * 64)

    with pytest.raises(SealedStudyError, match="does not correspond"):
        DevelopmentSelection.from_evaluation(
            mismatched_plan,
            mismatched_plan.candidate_fingerprints[0],
            development,
            selection_evidence="declared only",
        )


def test_final_firewall_one_shot_persistence_and_tamper_rejection(tmp_path: Path) -> None:
    plan, development, final = _plan(tmp_path)
    selection = DevelopmentSelection.from_evaluation(
        plan, plan.candidate_fingerprints[0], development, selection_evidence="rule"
    )
    sealed = seal_study(plan, selection)
    release = FinalRelease.from_evaluation(sealed, final)
    path = persist_final_release(tmp_path, release)
    assert load_final_release(tmp_path, sealed.sealed_study_id) == release
    with pytest.raises(SealedStudyError, match="already exists"):
        persist_final_release(tmp_path, release)
    path.chmod(0o644)
    path.write_bytes(path.read_bytes().replace(b'"descriptive_survival"', b'"descriptive_failure"'))
    with pytest.raises(SealedStudyError):
        load_final_release(tmp_path, sealed.sealed_study_id)


def test_malformed_persisted_release_rejects(tmp_path: Path) -> None:
    plan, development, final = _plan(tmp_path)
    selection = DevelopmentSelection.from_evaluation(
        plan, plan.candidate_fingerprints[0], development, selection_evidence="rule"
    )
    release = FinalRelease.from_evaluation(seal_study(plan, selection), final)
    path = persist_final_release(tmp_path, release)
    path.chmod(0o644)
    path.write_bytes(b"{")

    with pytest.raises(SealedStudyError, match="malformed"):
        load_final_release(tmp_path, release.sealed_study_id)


def test_tampered_selected_candidate_and_final_manifest_reject(tmp_path: Path) -> None:
    plan, development, final = _plan(tmp_path)
    selection = DevelopmentSelection.from_evaluation(
        plan, plan.candidate_fingerprints[0], development, selection_evidence="rule"
    )
    release = FinalRelease.from_evaluation(seal_study(plan, selection), final)
    path = persist_final_release(tmp_path, release)
    path.chmod(0o644)
    document = json.loads(path.read_bytes())
    tampered_selection = {
        **document,
        "development_selection": {
            **document["development_selection"],
            "selected_candidate_fingerprint": "0" * 64,
        },
    }
    path.write_text(
        json.dumps(tampered_selection, sort_keys=True, separators=(",", ":")), encoding="ascii"
    )

    with pytest.raises(SealedStudyError, match="sealed selected candidate"):
        load_final_release(tmp_path, release.sealed_study_id)

    tampered_manifest = {
        **document,
        "final_manifest": {
            **document["final_manifest"],
            "code_revision": "tampered-revision",
        },
    }
    path.write_text(
        json.dumps(tampered_manifest, sort_keys=True, separators=(",", ":")),
        encoding="ascii",
    )
    with pytest.raises(SealedStudyError, match="does not correspond"):
        load_final_release(tmp_path, release.sealed_study_id)


def test_candidate_set_and_minimum_criterion_mutations_change_identities(tmp_path: Path) -> None:
    plan, development, _ = _plan(tmp_path)
    candidate = plan.candidate_fingerprints[0]
    expanded_candidates = tuple(sorted((candidate, "f" * 64)))
    changed_candidates = replace(plan, candidate_fingerprints=expanded_candidates)
    changed_criterion = replace(
        plan, minimum_ordinary_trade_count=plan.minimum_ordinary_trade_count + 1
    )

    assert changed_candidates.candidate_set_id != plan.candidate_set_id
    assert changed_candidates.identity != plan.identity
    assert changed_criterion.identity != plan.identity
    original_sealed = seal_study(
        plan,
        DevelopmentSelection.from_evaluation(
            plan, candidate, development, selection_evidence="rule"
        ),
    )
    changed_sealed = seal_study(
        changed_candidates,
        DevelopmentSelection.from_evaluation(
            changed_candidates, candidate, development, selection_evidence="rule"
        ),
    )
    changed_criterion_sealed = seal_study(
        changed_criterion,
        DevelopmentSelection.from_evaluation(
            changed_criterion, candidate, development, selection_evidence="rule"
        ),
    )
    assert changed_sealed.sealed_study_id != original_sealed.sealed_study_id
    assert changed_criterion_sealed.sealed_study_id != original_sealed.sealed_study_id


def test_supersession_creates_new_identity_without_mutating_prior_evidence(tmp_path: Path) -> None:
    plan, development, final = _plan(tmp_path)
    selection = DevelopmentSelection.from_evaluation(
        plan, plan.candidate_fingerprints[0], development, selection_evidence="rule"
    )
    sealed = seal_study(plan, selection)
    release = FinalRelease.from_evaluation(sealed, final)
    persist_final_release(tmp_path, release)
    successor = replace(
        plan,
        supersedes_study_id=plan.identity,
        supersession_reason="corrected pre-final protocol",
    )

    assert successor.identity != plan.identity
    assert (
        seal_study(
            successor,
            DevelopmentSelection.from_evaluation(
                successor,
                successor.candidate_fingerprints[0],
                development,
                selection_evidence="rule",
            ),
        ).sealed_study_id
        != sealed.sealed_study_id
    )
    assert load_final_release(tmp_path, sealed.sealed_study_id) == release


def test_changed_final_dataset_cannot_satisfy_an_existing_sealed_study(tmp_path: Path) -> None:
    plan, development, final = _plan(tmp_path)
    selection = DevelopmentSelection.from_evaluation(
        plan, plan.candidate_fingerprints[0], development, selection_evidence="rule"
    )
    sealed = seal_study(plan, selection)
    changed_final = replace(plan.final_dataset, sha256="0" * 64)
    changed_evaluation = replace(
        final,
        manifest=replace(final.manifest, bar_dataset_fingerprint=changed_final.sha256),
    )
    assert (
        selection.selected_candidate_fingerprint
        == sealed.development_selection.selected_candidate_fingerprint
    )
    with pytest.raises(SealedStudyError, match="does not correspond"):
        FinalRelease.from_evaluation(sealed, changed_evaluation)


@pytest.mark.parametrize(
    ("count", "net", "minimum", "expected"),
    [
        (0, "1", 1, DescriptiveDisposition.INSUFFICIENT_EVIDENCE),
        (1, "0", 1, DescriptiveDisposition.DESCRIPTIVE_FAILURE),
        (1, "-1", 1, DescriptiveDisposition.DESCRIPTIVE_FAILURE),
        (1, "1", 1, DescriptiveDisposition.DESCRIPTIVE_SURVIVAL),
    ],
)
def test_descriptive_criterion_is_exact(
    count: int, net: str, minimum: int, expected: DescriptiveDisposition
) -> None:
    from decimal import Decimal

    assert descriptive_disposition(count, Decimal(net), minimum) is expected
