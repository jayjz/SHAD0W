"""BTC authority survives reopen in the same account-owned SQLite journal."""

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from shadow.adapters.alpaca.paper_identity import derive_paper_client_order_identity
from shadow.execution.btc_authority import (
    BtcAttempt,
    BtcRevalidation,
    BtcRunConfig,
    btc_intent_identity,
)
from shadow.execution.btc_journal import BtcJournal
from shadow.execution.crypto_accounting import CryptoActivityEvidence
from shadow.execution.journal import ExecutionJournal, JournalError
from shadow.execution.ownership import AccountOwner
from shadow.risk.btc import evaluate_btc_risk
from tests.test_btc_risk import inputs
from tests.test_paper_reconciliation import NOW


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[ExecutionJournal]:
    root = tmp_path / "owner"
    root.mkdir()
    with AccountOwner.acquire(ownership_directory=root, account_id="paper-account") as owner:
        with ExecutionJournal.create(
            path=tmp_path / "journal.sqlite",
            owner=owner,
            account_id="paper-account",
            operational_scope="scope",
            created_at=NOW,
        ) as value:
            yield value


def authority() -> tuple[BtcRunConfig, BtcAttempt]:
    values = inputs()
    evaluation = evaluate_btc_risk(**values)
    intent = btc_intent_identity(evaluation, "immutable-market-source")
    identity = derive_paper_client_order_identity(
        stable_account_binding="paper-account", operational_scope="scope", intent_identity=intent
    )
    evaluation = replace(
        evaluation, request=replace(evaluation.request, client_id=identity.client_order_id)
    )
    config = BtcRunConfig(
        "run",
        "immutable-market-source",
        evaluation.config.configuration_id,
        evaluation.policy.policy_id,
        "test-revision",
        2,
        2,
        86400,
    )
    snapshot = values["snapshot"]
    activities = CryptoActivityEvidence(
        snapshot.evidence,
        snapshot.history_start,
        snapshot.history_end,
        (),
        (),
        (),
        True,
        True,
        (),
        "synthetic-provider-coverage",
    )
    evidence = BtcRevalidation(
        values["intervals"],
        values["quote"],
        values["account"],
        values["asset"],
        snapshot,
        activities,
        values["controls"],
    )
    return config, BtcAttempt(
        intent, identity.full_digest, evaluation, evidence, NOW, NOW + timedelta(seconds=5), 2
    )


def ready(journal: ExecutionJournal) -> tuple[BtcJournal, BtcAttempt]:
    config, attempt = authority()
    store = BtcJournal(journal)
    store.configure(config)
    store.record_reconciliation(attempt.revalidation.snapshot, attempt.revalidation.activities)
    return store, attempt


def test_commit_restart_counts_binding_and_spent_attempt(journal: ExecutionJournal) -> None:
    store, attempt = ready(journal)
    store.commit(attempt, expected_revision=store.revision)
    journal.close()
    reopened = ExecutionJournal.reopen(
        path=journal.path,
        owner=journal._owner,
        account_id="paper-account",
        operational_scope="scope",
    )
    try:
        recovered = BtcJournal(reopened)
        assert recovered.attempts == (attempt,)
        assert not recovered.usable
        assert recovered.attempts[0].submission is None
        assert recovered.config is not None
        assert (
            sum(
                recovered.config.period(a.committed_at) == recovered.config.period(NOW)
                for a in recovered.attempts
            )
            == 1
        )
        with pytest.raises(JournalError):
            recovered.commit(attempt, expected_revision=recovered.revision)
        with pytest.raises(JournalError):
            recovered.configure(replace(recovered.config, run_id="restart-renamed"))
    finally:
        reopened.close()


def test_revision_conflict_halt_and_codec_replay(journal: ExecutionJournal) -> None:
    store, attempt = ready(journal)
    with pytest.raises(JournalError):
        store.commit(attempt, expected_revision=1)
    assert not store.attempts
    store.halt(NOW, "kill switch")
    recovered = BtcJournal(journal)
    assert recovered.halted
    with pytest.raises(JournalError):
        recovered.commit(attempt, expected_revision=recovered.revision)
    recovered.resume(expected_revision=recovered.revision)
    assert not recovered.halted
    recovered.commit(attempt, expected_revision=recovered.revision)
    journal.verify_projections()
    with pytest.raises(sqlite3.IntegrityError):
        journal._connection.execute("DELETE FROM btc_events")


def test_epoch_period_boundaries_are_explicit() -> None:
    config, _ = authority()
    midnight = NOW.replace(hour=0)
    assert config.period(midnight) == config.period(midnight + timedelta(days=1)) - 1
    assert config.period(midnight - timedelta(microseconds=1)) == config.period(midnight) - 1


def test_explicit_v4_migration_preserves_identity_and_rows(journal: ExecutionJournal) -> None:
    identity = journal.identity
    journal.record_halt(occurred_at=NOW, reason="existing-equity-halt")
    # Restore exact v4 shape as a migration fixture; never a production recovery path.
    connection = journal._connection
    connection.execute("DROP TRIGGER btc_events_immutable_update")
    connection.execute("DROP TRIGGER btc_events_immutable_delete")
    connection.execute("DROP TABLE btc_events")
    connection.execute("DROP TRIGGER journal_metadata_immutable_update")
    connection.execute("UPDATE journal_metadata SET schema_version='shadow.execution.journal.v4'")
    connection.execute(
        "CREATE TRIGGER journal_metadata_immutable_update BEFORE UPDATE ON journal_metadata "
        "BEGIN SELECT RAISE(ABORT, 'journal metadata is immutable'); END"
    )
    journal.close()
    with pytest.raises(JournalError):
        ExecutionJournal.reopen(
            path=journal.path,
            owner=journal._owner,
            account_id="paper-account",
            operational_scope="scope",
        )
    ExecutionJournal.migrate_v4(
        path=journal.path,
        owner=journal._owner,
        account_id="paper-account",
        operational_scope="scope",
    )
    with ExecutionJournal.reopen(
        path=journal.path,
        owner=journal._owner,
        account_id="paper-account",
        operational_scope="scope",
    ) as migrated:
        assert migrated.identity.journal_uuid == identity.journal_uuid
        assert migrated.identity.created_at == identity.created_at
        assert migrated._connection.execute("SELECT reason FROM journal_halts").fetchall() == [
            ("existing-equity-halt",)
        ]
        assert migrated.btc_events() == ()


def test_submission_ceilings_persist_across_reconstruction(journal: ExecutionJournal) -> None:
    from shadow.execution.broker import BrokerOrder, OrderStatus

    config, first = authority()
    store = BtcJournal(journal)
    store.configure(replace(config, maximum_per_period=1))
    store.record_reconciliation(first.revalidation.snapshot, first.revalidation.activities)
    store.commit(first, expected_revision=store.revision)
    request = first.request
    order = BrokerOrder(
        first.revalidation.snapshot.evidence,
        "original",
        request.client_id,
        request.instrument,
        request.side,
        request.quantity,
        request.quantity * 0,
        OrderStatus.REJECTED,
        request.order_type,
        request.time_in_force,
        False,
        None,
        None,
    )
    store.record_reconciliation(
        replace(first.revalidation.snapshot, orders=(order,)), first.revalidation.activities
    )
    store = BtcJournal(journal)
    evaluation = replace(
        first.evaluation,
        proposal=replace(
            first.evaluation.proposal,
            features=replace(
                first.evaluation.proposal.features,
                end_ns=first.evaluation.proposal.features.end_ns + 1,
            ),
        ),
    )
    intent = btc_intent_identity(evaluation, config.source_market_id)
    identity = derive_paper_client_order_identity(
        stable_account_binding=request.account_id,
        operational_scope=request.operational_scope,
        intent_identity=intent,
    )
    evaluation = replace(evaluation, request=replace(request, client_id=identity.client_order_id))
    second = replace(
        first,
        evaluation=evaluation,
        intent_identity=intent,
        client_order_full_digest=identity.full_digest,
        reconciliation_revision=store.reconciliation_revision,
    )
    with pytest.raises(JournalError, match="period submission ceiling"):
        store.commit(second, expected_revision=store.revision)
    second = replace(
        second,
        committed_at=NOW + timedelta(days=1),
        dispatch_deadline=NOW + timedelta(days=1, seconds=5),
    )
    store.commit(second, expected_revision=store.revision)
    assert len(BtcJournal(journal).attempts) == 2
    orders = (order, replace(order, order_id="second", client_id=second.client_order_id))
    later = NOW + timedelta(days=1)
    ev = replace(
        first.revalidation.snapshot.evidence, observation_time=later, availability_time=later
    )
    store.record_reconciliation(
        replace(first.revalidation.snapshot, evidence=ev, history_end=later, orders=orders),
        replace(first.revalidation.activities, evidence=ev, history_end=later),
    )
    evaluation = replace(
        evaluation,
        proposal=replace(
            evaluation.proposal,
            features=replace(
                evaluation.proposal.features,
                end_ns=evaluation.proposal.features.end_ns + 1,
                available_ns=evaluation.proposal.features.available_ns + 1,
            ),
        ),
    )
    intent = btc_intent_identity(evaluation, config.source_market_id)
    identity = derive_paper_client_order_identity(
        stable_account_binding=request.account_id,
        operational_scope=request.operational_scope,
        intent_identity=intent,
    )
    evaluation = replace(evaluation, request=replace(request, client_id=identity.client_order_id))
    third = replace(
        second,
        evaluation=evaluation,
        intent_identity=intent,
        client_order_full_digest=identity.full_digest,
        reconciliation_revision=store.reconciliation_revision,
    )
    with pytest.raises(JournalError, match="run submission ceiling"):
        BtcJournal(journal).commit(third, expected_revision=store.revision)
