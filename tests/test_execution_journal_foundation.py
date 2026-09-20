from __future__ import annotations

import hashlib
import inspect
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from shadow.adapters.alpaca import paper_identity
from shadow.adapters.alpaca.paper_identity import (
    PaperClientIdentityError,
    PaperClientIdentityRegistry,
    derive_paper_client_order_identity,
)
from shadow.domain import Instrument
from shadow.execution import journal, journal_codec, opportunity, ownership
from shadow.execution.journal import (
    JOURNAL_SCHEMA_VERSION,
    ExecutionJournal,
    JournalError,
)
from shadow.execution.journal_codec import (
    CODEC_VERSION,
    JournalCodecError,
    canonical_bytes,
    canonical_digest,
    decode_canonical,
)
from shadow.execution.opportunity import (
    SourceOpportunityBinding,
    SourceOpportunityError,
    SourceOpportunityKey,
    SourceOpportunityRegistry,
)
from shadow.execution.ownership import AccountOwner, OwnershipError
from shadow.features import (
    FeatureInput,
    FeatureName,
    FeatureSnapshot,
    FeatureState,
)
from shadow.risk.models import OperationalQuantityConfig, OrderIntent
from shadow.strategies import Signal, SignalReason, SignalType

NOW = datetime(2026, 9, 18, 15, tzinfo=UTC)
SPY = Instrument("SPY")


def binding(*, feature_value: Decimal = Decimal("-2.5")) -> SourceOpportunityBinding:
    feature = FeatureSnapshot(
        instrument=SPY,
        feature_name=FeatureName.Z_SCORE,
        input_value=FeatureInput.CLOSE,
        implementation_version="shadow.features.v1",
        value=feature_value,
        observation_time=NOW,
        availability_time=NOW,
        window=20,
        state=FeatureState.READY,
        unavailable_reason=None,
        source_dataset_id="feed-lineage-v1",
    )
    signal = Signal(
        instrument=SPY,
        strategy_id="mean_reversion_z_score",
        strategy_version="shadow.strategies.mean_reversion.v1",
        signal_type=SignalType.LONG_ENTRY,
        decision_time=NOW,
        availability_time=NOW,
        feature_name=FeatureName.Z_SCORE,
        feature_input=FeatureInput.CLOSE,
        feature_implementation_version="shadow.features.v1",
        feature_window=20,
        feature_observation_time=NOW,
        feature_availability_time=NOW,
        observed_feature_value=feature_value,
        configuration_id="strategy-v1",
        entry_threshold=Decimal("-2"),
        exit_threshold=Decimal("2"),
        maximum_feature_age=timedelta(minutes=1),
        reason=SignalReason.ENTRY_THRESHOLD,
        source_dataset_id="feed-lineage-v1",
    )
    intent = OrderIntent.from_signal(
        operational_scope="paper-scope",
        signal=signal,
        quantity_config=OperationalQuantityConfig(SPY, "quantity-v1", Decimal(1)),
    )
    key = SourceOpportunityKey(
        account_id="paper-account",
        operational_scope="paper-scope",
        feed_lineage="feed-lineage-v1",
        instrument=SPY,
        completed_bar_observation_time=NOW,
        strategy_id=signal.strategy_id,
        strategy_version=signal.strategy_version,
        feature_name=FeatureName.Z_SCORE,
        feature_input=FeatureInput.CLOSE,
        feature_implementation_version="shadow.features.v1",
        feature_window=20,
    )
    return SourceOpportunityBinding(key, feature, signal, intent)


def test_canonical_codec_replays_across_decimal_context_and_timezone() -> None:
    candidate = binding().intent
    with localcontext() as context:
        context.prec = 6
        first = canonical_bytes(candidate)
    with localcontext() as context:
        context.prec = 50
        second = canonical_bytes(candidate)
    assert first == second
    assert decode_canonical(first) == candidate
    offset = NOW.astimezone(timezone(timedelta(hours=-4)))
    normalized = Signal(
        instrument=SPY,
        strategy_id="mean_reversion_z_score",
        strategy_version="shadow.strategies.mean_reversion.v1",
        signal_type=SignalType.LONG_ENTRY,
        decision_time=offset,
        availability_time=offset,
        feature_name=FeatureName.Z_SCORE,
        feature_input=FeatureInput.CLOSE,
        feature_implementation_version="shadow.features.v1",
        feature_window=20,
        feature_observation_time=offset,
        feature_availability_time=offset,
        observed_feature_value=Decimal("-2.5"),
        configuration_id="strategy-v1",
        entry_threshold=Decimal("-2"),
        exit_threshold=Decimal("2"),
        maximum_feature_age=timedelta(minutes=1),
        reason=SignalReason.ENTRY_THRESHOLD,
        source_dataset_id="feed-lineage-v1",
    )
    assert canonical_bytes(normalized) == canonical_bytes(binding().signal)


def test_codec_rejects_unknown_or_noncanonical_payloads() -> None:
    payload = json.loads(canonical_bytes(binding().intent))
    payload["codec_version"] = "unknown"
    with pytest.raises(JournalCodecError):
        decode_canonical(json.dumps(payload, separators=(",", ":")).encode())
    with pytest.raises(JournalCodecError):
        canonical_bytes(1.0)


def test_source_binding_deduplicates_reconnect_and_rejects_material_variant() -> None:
    registry = SourceOpportunityRegistry()
    original = binding()
    assert registry.bind(original) is original
    reconnect = binding()
    assert registry.bind(reconnect) is original
    assert reconnect.key.source_key == original.key.source_key
    with pytest.raises(SourceOpportunityError, match="materially changed"):
        registry.bind(binding(feature_value=Decimal("-3")))


def test_paper_client_id_has_contract_vector_and_collision_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = derive_paper_client_order_identity(
        stable_account_binding="paper-account",
        operational_scope="paper-scope",
        intent_identity="a" * 64,
    )
    expected = hashlib.sha256(
        b'["shadow.alpaca.paper.client-order.v1","paper-account","paper-scope","'
        + b"a" * 64
        + b'"]'
    ).hexdigest()
    assert identity.full_digest == expected
    assert identity.client_order_id == "shp1_" + expected[:40]
    registry = PaperClientIdentityRegistry()
    registry.register(identity)
    monkeypatch.setattr(paper_identity, "_sha256_hex", lambda _payload: "b" * 64)
    first = derive_paper_client_order_identity(
        stable_account_binding="paper-account",
        operational_scope="paper-scope",
        intent_identity="first",
    )
    second = derive_paper_client_order_identity(
        stable_account_binding="paper-account",
        operational_scope="paper-scope",
        intent_identity="second",
    )
    registry = PaperClientIdentityRegistry()
    registry.register(first)
    with pytest.raises(PaperClientIdentityError, match="collision"):
        registry.register(second)


def _owner(directory: Path) -> AccountOwner:
    return AccountOwner.acquire(ownership_directory=directory, account_id="paper-account")


def _establish_journal(
    *,
    path: Path,
    ownership_directory: Path,
    account_id: str = "paper-account",
    scope: str = "paper-scope",
) -> None:
    ownership_directory.mkdir()
    with AccountOwner.acquire(
        ownership_directory=ownership_directory,
        account_id=account_id,
    ) as owner:
        created = ExecutionJournal.create(
            path=path,
            owner=owner,
            account_id=account_id,
            operational_scope=scope,
            created_at=NOW,
        )
        created.close()


def test_account_ownership_rejects_second_process_and_alternate_journal_path(
    tmp_path: Path,
) -> None:
    with _owner(tmp_path) as owner:
        with pytest.raises(OwnershipError):
            _owner(tmp_path)
        script = (
            "from pathlib import Path\n"
            "from shadow.execution.ownership import AccountOwner, OwnershipError\n"
            f"directory = Path({str(tmp_path)!r})\n"
            "try:\n"
            "    AccountOwner.acquire(ownership_directory=directory, account_id='paper-account')\n"
            "except OwnershipError:\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit(1)\n"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path.cwd() / "src")
        completed = subprocess.run([sys.executable, "-c", script], env=environment, check=False)
        assert completed.returncode == 0
        journal = ExecutionJournal.create(
            path=tmp_path / "one.sqlite",
            owner=owner,
            account_id="paper-account",
            operational_scope="paper-scope",
            created_at=NOW,
            journal_uuid="12345678-1234-4678-9234-567812345678",
        )
        journal.close()
        with pytest.raises(JournalError, match="conflicts with account ownership"):
            ExecutionJournal.create(
                path=tmp_path / "two.sqlite",
                owner=owner,
                account_id="paper-account",
                operational_scope="other-scope",
                created_at=NOW,
            )


def test_ownership_releases_after_process_exit_and_rejects_forked_handle(tmp_path: Path) -> None:
    script = (
        "from pathlib import Path\n"
        "from shadow.execution.ownership import AccountOwner\n"
        "owner = AccountOwner.acquire(\n"
        f"    ownership_directory=Path({str(tmp_path)!r}), account_id='paper-account'\n"
        ")\n"
        "owner.assert_held(account_id='paper-account')\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path.cwd() / "src")
    completed = subprocess.run([sys.executable, "-c", script], env=environment, check=True)
    assert completed.returncode == 0
    with _owner(tmp_path) as owner:
        if not hasattr(os, "fork"):
            pytest.skip("fork behavior is a Linux-only deployment constraint")
        child = os.fork()
        if child == 0:
            try:
                owner.assert_held(account_id="paper-account")
            except OwnershipError:
                os._exit(0)
            os._exit(1)
        _, status = os.waitpid(child, 0)
        assert os.waitstatus_to_exitcode(status) == 0


def test_journal_create_reopen_and_binding_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite"
    with _owner(tmp_path) as owner:
        created = ExecutionJournal.create(
            path=path,
            owner=owner,
            account_id="paper-account",
            operational_scope="paper-scope",
            created_at=NOW,
            journal_uuid="12345678-1234-4678-9234-567812345678",
        )
        assert created.identity.schema_version == JOURNAL_SCHEMA_VERSION
        assert created.identity.codec_version == CODEC_VERSION
        created.close()
        reopened = ExecutionJournal.reopen(
            path=path,
            owner=owner,
            account_id="paper-account",
            operational_scope="paper-scope",
        )
        assert reopened.identity.journal_uuid == "12345678-1234-4678-9234-567812345678"
        reopened.close()
        with pytest.raises(JournalError, match="binding mismatch"):
            ExecutionJournal.reopen(
                path=path,
                owner=owner,
                account_id="paper-account",
                operational_scope="other-scope",
            )
        with pytest.raises(JournalError, match="never creates"):
            ExecutionJournal.reopen(
                path=tmp_path / "missing.sqlite",
                owner=owner,
                account_id="paper-account",
                operational_scope="paper-scope",
            )


def test_journal_rejects_existing_incompatible_database(tmp_path: Path) -> None:
    path = tmp_path / "incompatible.sqlite"
    sqlite3.connect(path).close()
    with _owner(tmp_path) as owner:
        with pytest.raises(JournalError, match="incompatible"):
            ExecutionJournal.reopen(
                path=path,
                owner=owner,
                account_id="paper-account",
                operational_scope="paper-scope",
            )


def test_failed_incompatible_reopen_does_not_poison_correct_journal_binding(tmp_path: Path) -> None:
    incompatible = tmp_path / "incompatible.sqlite"
    correct = tmp_path / "correct.sqlite"
    sqlite3.connect(incompatible).close()
    _establish_journal(path=correct, ownership_directory=tmp_path / "provision")
    with _owner(tmp_path) as owner:
        with pytest.raises(JournalError, match="incompatible"):
            ExecutionJournal.reopen(
                path=incompatible,
                owner=owner,
                account_id="paper-account",
                operational_scope="paper-scope",
            )
        reopened = ExecutionJournal.reopen(
            path=correct,
            owner=owner,
            account_id="paper-account",
            operational_scope="paper-scope",
        )
        reopened.close()
        assert owner._journal_path == correct


@pytest.mark.parametrize(
    ("journal_account", "journal_scope", "requested_account", "requested_scope"),
    [
        ("other-account", "paper-scope", "paper-account", "paper-scope"),
        ("paper-account", "other-scope", "paper-account", "paper-scope"),
    ],
)
def test_failed_account_or_scope_reopen_does_not_poison_correct_journal_binding(
    tmp_path: Path,
    journal_account: str,
    journal_scope: str,
    requested_account: str,
    requested_scope: str,
) -> None:
    mismatched = tmp_path / "mismatched.sqlite"
    correct = tmp_path / "correct.sqlite"
    _establish_journal(
        path=mismatched,
        ownership_directory=tmp_path / "mismatched-provision",
        account_id=journal_account,
        scope=journal_scope,
    )
    _establish_journal(path=correct, ownership_directory=tmp_path / "correct-provision")
    with _owner(tmp_path) as owner:
        with pytest.raises(JournalError, match="binding mismatch"):
            ExecutionJournal.reopen(
                path=mismatched,
                owner=owner,
                account_id=requested_account,
                operational_scope=requested_scope,
            )
        reopened = ExecutionJournal.reopen(
            path=correct,
            owner=owner,
            account_id="paper-account",
            operational_scope="paper-scope",
        )
        reopened.close()
        assert owner._journal_path == correct


def test_failed_create_does_not_claim_an_unestablished_journal_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed = tmp_path / "failed.sqlite"
    correct = tmp_path / "correct.sqlite"
    original_create_schema = ExecutionJournal._create_schema

    def fail_schema(_connection: sqlite3.Connection, _identity: object) -> None:
        raise JournalError("injected schema initialization failure")

    monkeypatch.setattr(ExecutionJournal, "_create_schema", staticmethod(fail_schema))
    with _owner(tmp_path) as owner:
        with pytest.raises(JournalError, match="metadata creation failed"):
            ExecutionJournal.create(
                path=failed,
                owner=owner,
                account_id="paper-account",
                operational_scope="paper-scope",
                created_at=NOW,
            )
        monkeypatch.setattr(
            ExecutionJournal,
            "_create_schema",
            staticmethod(original_create_schema),
        )
        created = ExecutionJournal.create(
            path=correct,
            owner=owner,
            account_id="paper-account",
            operational_scope="paper-scope",
            created_at=NOW,
        )
        created.close()
        assert owner._journal_path == correct
        assert failed.exists()


def test_successful_journal_binding_accepts_only_its_single_valid_path(tmp_path: Path) -> None:
    first = tmp_path / "first.sqlite"
    alternate = tmp_path / "alternate.sqlite"
    _establish_journal(path=alternate, ownership_directory=tmp_path / "alternate-provision")
    with _owner(tmp_path) as owner:
        created = ExecutionJournal.create(
            path=first,
            owner=owner,
            account_id="paper-account",
            operational_scope="paper-scope",
            created_at=NOW,
        )
        created.close()
        assert owner._journal_path == first
        reopened = ExecutionJournal.reopen(
            path=first,
            owner=owner,
            account_id="paper-account",
            operational_scope="paper-scope",
        )
        reopened.close()
        with pytest.raises(JournalError, match="conflicts with account ownership"):
            ExecutionJournal.reopen(
                path=alternate,
                owner=owner,
                account_id="paper-account",
                operational_scope="paper-scope",
            )


@pytest.mark.parametrize("column", ["schema_version", "codec_version"])
def test_journal_rejects_unsupported_schema_or_codec_version(tmp_path: Path, column: str) -> None:
    path = tmp_path / "journal.sqlite"
    with _owner(tmp_path) as owner:
        journal = ExecutionJournal.create(
            path=path,
            owner=owner,
            account_id="paper-account",
            operational_scope="paper-scope",
            created_at=NOW,
            journal_uuid="12345678-1234-4678-9234-567812345678",
        )
        journal.close()
        connection = sqlite3.connect(path)
        connection.execute("DROP TRIGGER journal_metadata_immutable_update")
        connection.execute(f"UPDATE journal_metadata SET {column} = 'unsupported'")
        connection.execute(
            """
            CREATE TRIGGER journal_metadata_immutable_update
            BEFORE UPDATE ON journal_metadata
            BEGIN SELECT RAISE(ABORT, 'journal metadata is immutable'); END
            """
        )
        connection.commit()
        connection.close()
        with pytest.raises(JournalError, match="unsupported journal schema or codec version"):
            ExecutionJournal.reopen(
                path=path,
                owner=owner,
                account_id="paper-account",
                operational_scope="paper-scope",
            )


def test_new_modules_cannot_issue_network_or_broker_calls() -> None:
    import ast

    modules = (journal, journal_codec, opportunity, ownership, paper_identity)
    forbidden_import_roots = {"http", "requests", "socket", "urllib", "websockets"}
    forbidden_calls = {
        "read_account",
        "read_asset",
        "read_clock",
        "read_snapshot",
        "read_updates",
        "lookup_order",
        "submit",
    }
    for module in modules:
        tree = ast.parse(inspect.getsource(module))
        imported = {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        assert not imported & forbidden_import_roots
        assert (
            not {
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            }
            & forbidden_calls
        )
    assert len(canonical_digest(binding().intent)) == 64
