from dataclasses import replace
from decimal import Decimal

from shadow.execution.broker import BrokerPosition
from shadow.execution.btc_classification import (
    AuthorityClassification,
    classify_btc_authority,
)
from shadow.execution.journal import ExecutionJournal
from shadow.execution.reconciliation import OperationalState, Reconciliation
from tests.test_btc_journal import authority
from tests.test_btc_journal import journal as journal


def test_fee_finality_alone_allows_initial_entry_experiment_but_not_proof() -> None:
    _, attempt = authority()
    evidence = replace(
        attempt.revalidation.activities, history_verified=False, coverage_reference=None
    )
    unresolved = Reconciliation(
        OperationalState.UNRESOLVED, Decimal(0), (), "activity history unproven"
    )
    status = classify_btc_authority(
        account=attempt.revalidation.account,
        asset=attempt.revalidation.asset,
        snapshot=attempt.revalidation.snapshot,
        activities=evidence,
        strict_lifecycle=unresolved,
        account_binding=(attempt.request.account_id, attempt.request.operational_scope),
        journal_usable=True,
        risk_fresh=True,
        unresolved_prior_intent=False,
    )
    assert status.classification is AuthorityClassification.EXPERIMENT_READY
    assert status.proof_status is AuthorityClassification.BLOCKED
    assert status.proof_blocker == "provider_fee_linkage_finality"
    assert status.observed_position is None


def test_broker_contradiction_blocks_experiment_readiness() -> None:
    _, attempt = authority()
    evidence = replace(
        attempt.revalidation.activities, history_verified=False, coverage_reference=None
    )
    unresolved = Reconciliation(
        OperationalState.UNRESOLVED, Decimal(0), (), "activity history unproven"
    )
    snapshot = attempt.revalidation.snapshot
    # Any pre-existing broker order or position invalidates the one-entry seam.
    if not snapshot.positions:
        snapshot = replace(
            snapshot,
            positions=(
                BrokerPosition(
                    snapshot.evidence, attempt.request.instrument, attempt.request.quantity
                ),
            ),
        )
    status = classify_btc_authority(
        account=attempt.revalidation.account,
        asset=attempt.revalidation.asset,
        snapshot=snapshot,
        activities=evidence,
        strict_lifecycle=unresolved,
        account_binding=(attempt.request.account_id, attempt.request.operational_scope),
        journal_usable=True,
        risk_fresh=True,
        unresolved_prior_intent=False,
    )
    assert status.classification is AuthorityClassification.BLOCKED
    assert "broker_state_not_initial_flat" in status.experiment_blockers


def test_real_journal_allows_only_pristine_unverified_history_cut(
    journal: ExecutionJournal,
) -> None:
    from shadow.execution.btc_journal import BtcJournal

    config, attempt = authority()
    store = BtcJournal(journal)
    store.configure(config)
    activities = replace(
        attempt.revalidation.activities, history_verified=False, coverage_reference=None
    )
    from shadow.execution.btc_classification import proof_reconcile

    proof = proof_reconcile(
        attempts=(), snapshot=attempt.revalidation.snapshot, activities=activities
    )
    assert proof.state is OperationalState.UNRESOLVED
    store.record_reconciliation(attempt.revalidation.snapshot, activities)
    assert store.execution_authority_ready
    assert not store.usable
    status = classify_btc_authority(
        account=attempt.revalidation.account,
        asset=attempt.revalidation.asset,
        snapshot=attempt.revalidation.snapshot,
        activities=activities,
        strict_lifecycle=proof,
        account_binding=(attempt.request.account_id, attempt.request.operational_scope),
        journal_usable=store.execution_authority_ready,
        risk_fresh=True,
        unresolved_prior_intent=bool(store.attempts),
    )
    assert status.classification is AuthorityClassification.EXPERIMENT_READY
    assert status.proof_blocker == "provider_fee_linkage_finality"


def test_unverified_history_is_never_proof_ready() -> None:
    _, attempt = authority()
    activities = replace(
        attempt.revalidation.activities, history_verified=False, coverage_reference=None
    )
    from shadow.execution.btc_classification import proof_reconcile

    result = proof_reconcile(
        attempts=(), snapshot=attempt.revalidation.snapshot, activities=activities
    )
    assert result.state is OperationalState.UNRESOLVED
