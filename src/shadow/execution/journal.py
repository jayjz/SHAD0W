# ruff: noqa: E501
"""P5A.2 durable journal identity and immutable source/decision projections."""

from __future__ import annotations

import os
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from shadow.execution.broker import (
    BrokerAccount,
    BrokerAsset,
    BrokerClock,
    BrokerSnapshot,
    SubmissionResult,
    SubmitRequest,
)
from shadow.execution.journal_codec import CODEC_VERSION, canonical_bytes, decode_canonical
from shadow.execution.opportunity import SourceOpportunityBinding, SourceOpportunityError
from shadow.execution.ownership import AccountOwner, OwnershipError
from shadow.features import FeatureSnapshot
from shadow.risk.models import OrderIntent, OrderTarget, RiskDecision
from shadow.strategies import Signal

JOURNAL_SCHEMA_VERSION = "shadow.execution.journal.v5"
PREVIOUS_JOURNAL_SCHEMA_VERSION = "shadow.execution.journal.v4"
T = TypeVar("T")


class JournalError(RuntimeError):
    """The local durable journal cannot establish its immutable identity."""


@dataclass(frozen=True, slots=True)
class CommittedAttempt:
    """One permanently-spent network attempt, reconstructed without ambient state."""

    intent_identity: str
    source_opportunity_id: str
    client_order_id: str
    client_order_full_digest: str
    broker_trading_date: str
    attempt_sequence: int
    committed_at: datetime
    dispatch_deadline: datetime
    request: SubmitRequest
    risk_decision: RiskDecision
    account: BrokerAccount
    clock: BrokerClock
    asset: BrokerAsset
    snapshot: BrokerSnapshot
    submission: SubmissionResult | None
    reconciliation: BrokerSnapshot | None

    def __post_init__(self) -> None:
        for name in (
            "intent_identity",
            "source_opportunity_id",
            "client_order_id",
            "broker_trading_date",
        ):
            _text(getattr(self, name), name)
        if len(self.client_order_full_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.client_order_full_digest
        ):
            raise JournalError("client order full digest must be a SHA-256 digest")
        if self.attempt_sequence <= 0:
            raise JournalError("attempt_sequence must be positive")
        object.__setattr__(self, "committed_at", _utc(self.committed_at, "committed_at"))
        object.__setattr__(
            self, "dispatch_deadline", _utc(self.dispatch_deadline, "dispatch_deadline")
        )
        if self.dispatch_deadline < self.committed_at:
            raise JournalError("dispatch deadline precedes commit")


@dataclass(frozen=True, slots=True)
class JournalAdmission:
    """The first durable decision for one immutable source opportunity.

    This is evidence and a projection only.  It deliberately creates no
    capability, reservation, reconciliation state, or dispatch authority.
    """

    source_key: str
    binding_digest: str
    intent_identity: str
    decision: RiskDecision

    def __post_init__(self) -> None:
        for name in ("source_key", "binding_digest", "intent_identity"):
            _text(getattr(self, name), name)
        if len(self.binding_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.binding_digest
        ):
            raise JournalError("binding_digest must be a SHA-256 digest")
        if not isinstance(self.decision, RiskDecision):
            raise JournalError("decision must be a RiskDecision")
        if self.decision.intent.intent_identity != self.intent_identity:
            raise JournalError("decision intent identity mismatch")


def _text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise JournalError(f"{field} must be a nonempty trimmed string")


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is not UTC:
        raise JournalError(f"{field} must use datetime.UTC")
    return value


def _database_path(path: Path, *, exists: bool) -> Path:
    try:
        parent = path.parent.absolute()
        if parent.is_symlink():
            raise JournalError("journal parent must not be a symlink")
        parent = parent.resolve(strict=True)
    except OSError as exc:
        raise JournalError("journal parent is unavailable") from exc
    if not parent.is_dir():
        raise JournalError("journal parent must be a directory")
    if path.parent.absolute() != parent:
        raise JournalError("journal parent path aliases are unsupported")
    candidate = parent / path.name
    if candidate.name in {"", ".", ".."} or candidate.is_symlink():
        raise JournalError("journal path must be a direct non-symlink file")
    if exists:
        try:
            file_stat = os.stat(candidate, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise JournalError("REOPEN never creates a missing journal") from exc
        if not stat.S_ISREG(file_stat.st_mode):
            raise JournalError("journal path must be a regular file")
    elif candidate.exists():
        raise JournalError("CREATE refuses an existing journal")
    return candidate


def _connect(path: Path, *, create: bool) -> sqlite3.Connection:
    if create:
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise JournalError("unable to create journal exclusively") from exc
        os.close(fd)
    uri = f"file:{path}?mode=rw"
    try:
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    except sqlite3.Error as exc:
        raise JournalError("unable to open journal") from exc
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise JournalError("SQLite foreign keys are unavailable")
        mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
        if mode != ("delete",):
            raise JournalError("SQLite DELETE journaling is unavailable")
        connection.execute("PRAGMA synchronous = EXTRA")
        if connection.execute("PRAGMA synchronous").fetchone() != (3,):
            raise JournalError("SQLite EXTRA synchronous durability is unavailable")
    except (sqlite3.Error, JournalError):
        connection.close()
        raise
    return connection


@dataclass(frozen=True, slots=True)
class JournalIdentity:
    journal_uuid: str
    account_id: str
    operational_scope: str
    target: OrderTarget
    schema_version: str
    codec_version: str
    created_at: datetime

    def __post_init__(self) -> None:
        try:
            parsed = uuid.UUID(self.journal_uuid)
        except (ValueError, AttributeError) as exc:
            raise JournalError("journal_uuid must be a UUID") from exc
        if str(parsed) != self.journal_uuid:
            raise JournalError("journal_uuid must be canonical")
        for name in ("account_id", "operational_scope", "schema_version", "codec_version"):
            _text(getattr(self, name), name)
        if self.target is not OrderTarget.PAPER:
            raise JournalError("only the paper target is supported")
        _utc(self.created_at, "created_at")


@dataclass(slots=True)
class ExecutionJournal:
    """A held local connection with immutable P5A.2A journal metadata only."""

    path: Path
    identity: JournalIdentity
    _connection: sqlite3.Connection
    _owner: AccountOwner
    _closed: bool = False

    @classmethod
    def create(
        cls,
        *,
        path: Path,
        owner: AccountOwner,
        account_id: str,
        operational_scope: str,
        created_at: datetime,
        journal_uuid: str | None = None,
    ) -> ExecutionJournal:
        try:
            owner.assert_held(account_id=account_id)
        except OwnershipError as exc:
            raise JournalError("journal creation requires account ownership") from exc
        candidate = _database_path(path, exists=False)
        identity = JournalIdentity(
            journal_uuid=str(uuid.uuid4()) if journal_uuid is None else journal_uuid,
            account_id=account_id,
            operational_scope=operational_scope,
            target=OrderTarget.PAPER,
            schema_version=JOURNAL_SCHEMA_VERSION,
            codec_version=CODEC_VERSION,
            created_at=created_at,
        )
        connection = _connect(candidate, create=True)
        try:
            cls._create_schema(connection, identity)
            if cls._read_identity(connection) != identity:
                raise JournalError("created journal identity verification failed")
        except (sqlite3.Error, JournalError) as exc:
            connection.close()
            raise JournalError("journal metadata creation failed") from exc
        try:
            owner.bind_journal_path(journal_path=candidate)
        except OwnershipError as exc:
            connection.close()
            raise JournalError("journal path conflicts with account ownership") from exc
        journal = cls(candidate, identity, connection, owner)
        journal.verify_projections()
        return journal

    @classmethod
    def reopen(
        cls,
        *,
        path: Path,
        owner: AccountOwner,
        account_id: str,
        operational_scope: str,
    ) -> ExecutionJournal:
        try:
            owner.assert_held(account_id=account_id)
        except OwnershipError as exc:
            raise JournalError("journal reopen requires account ownership") from exc
        candidate = _database_path(path, exists=True)
        connection = _connect(candidate, create=False)
        try:
            identity = cls._read_identity(connection)
            if (identity.account_id, identity.operational_scope) != (account_id, operational_scope):
                raise JournalError("journal account/scope binding mismatch")
        except (sqlite3.Error, JournalError):
            connection.close()
            raise
        try:
            owner.bind_journal_path(journal_path=candidate)
        except OwnershipError as exc:
            connection.close()
            raise JournalError("journal path conflicts with account ownership") from exc
        journal = cls(candidate, identity, connection, owner)
        try:
            journal.verify_projections()
        except JournalError:
            journal.close()
            raise
        return journal

    @staticmethod
    def _create_schema(connection: sqlite3.Connection, identity: JournalIdentity) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                CREATE TABLE journal_metadata (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    journal_uuid TEXT NOT NULL UNIQUE,
                    account_id TEXT NOT NULL,
                    operational_scope TEXT NOT NULL,
                    target TEXT NOT NULL CHECK (target = 'paper'),
                    schema_version TEXT NOT NULL,
                    codec_version TEXT NOT NULL,
                    created_at TEXT NOT NULL
                ) STRICT
                """
            )
            connection.execute(
                """
                CREATE TRIGGER journal_metadata_immutable_update
                BEFORE UPDATE ON journal_metadata
                BEGIN SELECT RAISE(ABORT, 'journal metadata is immutable'); END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER journal_metadata_immutable_delete
                BEFORE DELETE ON journal_metadata
                BEGIN SELECT RAISE(ABORT, 'journal metadata is immutable'); END
                """
            )
            connection.execute(
                """
                INSERT INTO journal_metadata
                VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.journal_uuid,
                    identity.account_id,
                    identity.operational_scope,
                    identity.target.value,
                    identity.schema_version,
                    identity.codec_version,
                    identity.created_at.isoformat(),
                ),
            )
            connection.execute(
                """
                CREATE TABLE committed_attempts (
                    intent_identity TEXT PRIMARY KEY,
                    source_opportunity_id TEXT NOT NULL UNIQUE,
                    client_order_id TEXT NOT NULL UNIQUE,
                    client_order_full_digest TEXT NOT NULL UNIQUE,
                    broker_trading_date TEXT NOT NULL,
                    attempt_sequence INTEGER NOT NULL UNIQUE CHECK (attempt_sequence > 0),
                    committed_at TEXT NOT NULL,
                    dispatch_deadline TEXT NOT NULL,
                    request BLOB NOT NULL,
                    risk_decision BLOB NOT NULL,
                    account BLOB NOT NULL,
                    clock BLOB NOT NULL,
                    asset BLOB NOT NULL,
                    snapshot BLOB NOT NULL,
                    submission BLOB,
                    reconciliation BLOB
                ) STRICT
                """
            )
            connection.execute(
                """
                CREATE TABLE journal_halts (
                    sequence INTEGER PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    intent_identity TEXT
                ) STRICT
                """
            )
            connection.execute(
                """
                CREATE TABLE source_opportunity_bindings (
                    source_key TEXT PRIMARY KEY,
                    binding_digest TEXT NOT NULL,
                    source_key_evidence BLOB NOT NULL,
                    feature BLOB NOT NULL,
                    signal BLOB NOT NULL,
                    intent BLOB NOT NULL
                ) STRICT
                """
            )
            connection.execute(
                """
                CREATE TABLE terminal_admissions (
                    source_key TEXT PRIMARY KEY,
                    binding_digest TEXT NOT NULL,
                    intent_identity TEXT NOT NULL UNIQUE,
                    decision BLOB NOT NULL,
                    FOREIGN KEY (source_key) REFERENCES source_opportunity_bindings(source_key)
                ) STRICT
                """
            )
            connection.execute(
                """
                CREATE TABLE journal_events (
                    sequence INTEGER PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('source_bound', 'admission_recorded')),
                    source_key TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    FOREIGN KEY (source_key) REFERENCES source_opportunity_bindings(source_key)
                ) STRICT
                """
            )
            for table in (
                "source_opportunity_bindings",
                "terminal_admissions",
                "journal_events",
            ):
                connection.execute(
                    f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'journal evidence is immutable'); END"
                )
                connection.execute(
                    f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'journal evidence is immutable'); END"
                )
            connection.execute(
                """
                CREATE TRIGGER committed_attempts_transition_only
                BEFORE UPDATE ON committed_attempts
                WHEN NOT (
                    (
                        NEW.intent_identity IS OLD.intent_identity
                        AND NEW.source_opportunity_id IS OLD.source_opportunity_id
                        AND NEW.client_order_id IS OLD.client_order_id
                        AND NEW.client_order_full_digest IS OLD.client_order_full_digest
                        AND NEW.broker_trading_date IS OLD.broker_trading_date
                        AND NEW.attempt_sequence IS OLD.attempt_sequence
                        AND NEW.committed_at IS OLD.committed_at
                        AND NEW.dispatch_deadline IS OLD.dispatch_deadline
                        AND NEW.request IS OLD.request
                        AND NEW.risk_decision IS OLD.risk_decision
                        AND NEW.account IS OLD.account
                        AND NEW.clock IS OLD.clock
                        AND NEW.asset IS OLD.asset
                        AND NEW.snapshot IS OLD.snapshot
                        AND OLD.submission IS NULL
                        AND NEW.submission IS NOT NULL
                        AND NEW.reconciliation IS OLD.reconciliation
                    )
                    OR (
                        NEW.intent_identity IS OLD.intent_identity
                        AND NEW.source_opportunity_id IS OLD.source_opportunity_id
                        AND NEW.client_order_id IS OLD.client_order_id
                        AND NEW.client_order_full_digest IS OLD.client_order_full_digest
                        AND NEW.broker_trading_date IS OLD.broker_trading_date
                        AND NEW.attempt_sequence IS OLD.attempt_sequence
                        AND NEW.committed_at IS OLD.committed_at
                        AND NEW.dispatch_deadline IS OLD.dispatch_deadline
                        AND NEW.request IS OLD.request
                        AND NEW.risk_decision IS OLD.risk_decision
                        AND NEW.account IS OLD.account
                        AND NEW.clock IS OLD.clock
                        AND NEW.asset IS OLD.asset
                        AND NEW.snapshot IS OLD.snapshot
                        AND NEW.submission IS OLD.submission
                        AND OLD.reconciliation IS NULL
                        AND NEW.reconciliation IS NOT NULL
                    )
                )
                BEGIN SELECT RAISE(ABORT, 'committed attempts are transition-only'); END
                """
            )
            ExecutionJournal._create_btc_schema(connection)
            connection.execute("COMMIT")
        except sqlite3.Error:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _create_btc_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE btc_events (revision INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload BLOB NOT NULL) STRICT"
        )
        for operation in ("update", "delete"):
            connection.execute(
                f"CREATE TRIGGER btc_events_immutable_{operation} BEFORE {operation.upper()} ON btc_events "
                "BEGIN SELECT RAISE(ABORT, 'BTC evidence is immutable'); END"
            )

    @classmethod
    def migrate_v4(
        cls, *, path: Path, owner: AccountOwner, account_id: str, operational_scope: str
    ) -> None:
        """Explicit additive v4 -> v5 transaction, preserving all existing rows."""
        owner.assert_held(account_id=account_id)
        candidate = _database_path(path, exists=True)
        connection = _connect(candidate, create=False)
        try:
            identity = cls._read_identity(connection, version=PREVIOUS_JOURNAL_SCHEMA_VERSION)
            if (identity.account_id, identity.operational_scope) != (account_id, operational_scope):
                raise JournalError("migration binding mismatch")
            owner.bind_journal_path(journal_path=candidate)
            # Validate v4 projections before changing its schema.
            cls(candidate, identity, connection, owner).verify_projections()
            connection.execute("BEGIN IMMEDIATE")
            cls._create_btc_schema(connection)
            connection.execute("DROP TRIGGER journal_metadata_immutable_update")
            connection.execute(
                "UPDATE journal_metadata SET schema_version = ?", (JOURNAL_SCHEMA_VERSION,)
            )
            connection.execute(
                "CREATE TRIGGER journal_metadata_immutable_update BEFORE UPDATE ON journal_metadata "
                "BEGIN SELECT RAISE(ABORT, 'journal metadata is immutable'); END"
            )
            cls._read_identity(connection)
            connection.execute("COMMIT")
        except (sqlite3.Error, JournalError) as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise JournalError("explicit v4 migration failed") from exc
        finally:
            connection.close()

    def application_events(self, namespace: str) -> tuple[object, ...]:
        """Owned application evidence, separate from execution authority projections."""
        self.assert_held()
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS application_events ("
            "sequence INTEGER PRIMARY KEY, namespace TEXT NOT NULL, payload BLOB NOT NULL) STRICT"
        )
        return tuple(
            decode_canonical(bytes(row[0]))
            for row in self._connection.execute(
                "SELECT payload FROM application_events WHERE namespace=? ORDER BY sequence",
                (namespace,),
            )
        )

    def append_application_event(self, namespace: str, payload: object) -> None:
        self._transaction()
        try:
            self._connection.execute(
                "INSERT INTO application_events(namespace,payload) VALUES (?,?)",
                (namespace, canonical_bytes(payload)),
            )
            self._commit()
        except Exception:
            self._rollback()
            raise

    def has_legacy_halts(self) -> bool:
        """Existing account-wide halts also freeze BTC; migration cannot clear them."""
        self.assert_held()
        try:
            return (
                self._connection.execute("SELECT 1 FROM journal_halts LIMIT 1").fetchone()
                is not None
            )
        except sqlite3.Error as exc:
            raise JournalError("cannot establish account halt state") from exc

    def btc_events(self) -> tuple[tuple[int, str, object], ...]:
        self.assert_held()
        try:
            rows = self._connection.execute(
                "SELECT revision, kind, payload FROM btc_events ORDER BY revision"
            ).fetchall()
            return tuple(
                (revision, kind, decode_canonical(payload)) for revision, kind, payload in rows
            )
        except (sqlite3.Error, ValueError) as exc:
            raise JournalError("BTC journal replay failed") from exc

    def append_btc_event(self, *, expected_revision: int, kind: str, payload: object) -> int:
        """Serialized compare-and-append; reducers validate transition semantics."""
        self._transaction()
        try:
            actual = self._connection.execute(
                "SELECT COALESCE(MAX(revision), 0) FROM btc_events"
            ).fetchone()[0]
            if type(actual) is not int:
                raise JournalError("invalid BTC revision")
            if actual != expected_revision:
                raise JournalError("BTC journal revision conflict")
            self._connection.execute(
                "INSERT INTO btc_events VALUES (?, ?, ?)",
                (actual + 1, kind, self._encoded(payload)),
            )
            self._commit()
            return actual + 1
        except (sqlite3.Error, JournalError) as exc:
            self._rollback()
            raise JournalError("BTC event commit failed") from exc

    @staticmethod
    def _read_identity(
        connection: sqlite3.Connection, *, version: str = JOURNAL_SCHEMA_VERSION
    ) -> JournalIdentity:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise JournalError("journal integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise JournalError("journal foreign key check failed")
        expected_objects = {
            ("table", "committed_attempts"),
            ("table", "journal_metadata"),
            ("table", "journal_halts"),
            ("table", "journal_events"),
            ("table", "source_opportunity_bindings"),
            ("table", "terminal_admissions"),
            ("trigger", "committed_attempts_transition_only"),
            ("trigger", "journal_events_immutable_delete"),
            ("trigger", "journal_events_immutable_update"),
            ("trigger", "journal_metadata_immutable_delete"),
            ("trigger", "journal_metadata_immutable_update"),
            ("trigger", "source_opportunity_bindings_immutable_delete"),
            ("trigger", "source_opportunity_bindings_immutable_update"),
            ("trigger", "terminal_admissions_immutable_delete"),
            ("trigger", "terminal_admissions_immutable_update"),
        }
        if version == JOURNAL_SCHEMA_VERSION:
            expected_objects.update(
                {
                    ("table", "btc_events"),
                    ("trigger", "btc_events_immutable_update"),
                    ("trigger", "btc_events_immutable_delete"),
                }
            )
        objects = set(
            connection.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
        )
        if objects != expected_objects:
            raise JournalError("unsupported or incompatible journal schema")
        rows = connection.execute(
            "SELECT journal_uuid, account_id, operational_scope, target, schema_version, "
            "codec_version, created_at FROM journal_metadata"
        ).fetchall()
        if len(rows) != 1:
            raise JournalError("journal metadata is missing or ambiguous")
        row = rows[0]
        if row[4] != version or row[5] != CODEC_VERSION:
            raise JournalError("unsupported journal schema or codec version")
        try:
            created_at = datetime.fromisoformat(row[6])
        except (TypeError, ValueError) as exc:
            raise JournalError("invalid journal creation timestamp") from exc
        return JournalIdentity(
            journal_uuid=row[0],
            account_id=row[1],
            operational_scope=row[2],
            target=OrderTarget(row[3]),
            schema_version=row[4],
            codec_version=row[5],
            created_at=created_at,
        )

    def assert_held(self) -> None:
        if self._closed:
            raise JournalError("journal is closed")
        try:
            self._owner.assert_held(account_id=self.identity.account_id)
        except OwnershipError as exc:
            raise JournalError("journal ownership is no longer valid") from exc

    def _transaction(self) -> None:
        self.assert_held()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise JournalError("unable to begin durable journal transaction") from exc

    def _commit(self) -> None:
        try:
            self._connection.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                self._connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise JournalError("durable journal commit failed") from exc

    def _rollback(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    @staticmethod
    def _encoded(value: object) -> bytes:
        try:
            return canonical_bytes(value)
        except ValueError as exc:
            raise JournalError("journal evidence cannot be canonically encoded") from exc

    @staticmethod
    def _decoded(value: object, expected: type[T]) -> T:
        if not isinstance(value, bytes):
            raise JournalError("journal evidence payload is malformed")
        try:
            decoded = decode_canonical(value)
        except ValueError as exc:
            raise JournalError("journal evidence cannot be decoded") from exc
        if not isinstance(decoded, expected):
            raise JournalError("journal evidence type mismatch")
        return decoded

    @staticmethod
    def _binding_key_evidence(binding: SourceOpportunityBinding) -> tuple[object, ...]:
        key = binding.key
        return (
            "shadow.source-opportunity-key.v2",
            key.account_id,
            key.operational_scope,
            key.feed_lineage,
            key.instrument,
            key.completed_bar_observation_time,
            key.strategy_id,
            key.strategy_version,
            key.strategy_configuration_id,
            key.signal_type,
            key.feature_name,
            key.feature_input,
            key.feature_implementation_version,
            key.feature_window,
        )

    def bind_source_opportunity(
        self, binding: SourceOpportunityBinding
    ) -> SourceOpportunityBinding:
        """Persist the first causal binding; exact reconnect redelivery is idempotent.

        A changed feature, signal, intent, or source key is a durable conflict.  The
        method is intentionally below admission: it has no broker or capability
        interaction and cannot make an order possible.
        """
        if not isinstance(binding, SourceOpportunityBinding):
            raise JournalError("binding must be a SourceOpportunityBinding")
        source_key = binding.key.source_key
        binding_digest = binding.evidence_digest
        key_evidence = self._encoded(self._binding_key_evidence(binding))
        feature = self._encoded(binding.feature)
        signal = self._encoded(binding.signal)
        intent = self._encoded(binding.intent)
        self._transaction()
        try:
            existing = self._connection.execute(
                "SELECT binding_digest, source_key_evidence, feature, signal, intent "
                "FROM source_opportunity_bindings WHERE source_key = ?",
                (source_key,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (binding_digest, key_evidence, feature, signal, intent):
                    raise JournalError("materially changed source opportunity binding")
                self._commit()
                return binding
            self._connection.execute(
                "INSERT INTO source_opportunity_bindings VALUES (?, ?, ?, ?, ?, ?)",
                (source_key, binding_digest, key_evidence, feature, signal, intent),
            )
            self._connection.execute(
                "INSERT INTO journal_events (occurred_at, kind, source_key, payload) "
                "VALUES (?, 'source_bound', ?, ?)",
                (
                    binding.signal.decision_time.isoformat(),
                    source_key,
                    self._encoded(binding_digest),
                ),
            )
            self._commit()
        except (sqlite3.Error, JournalError):
            self._rollback()
            raise
        return binding

    def source_binding(self, source_key: str) -> SourceOpportunityBinding | None:
        """Replay one binding strictly from journal bytes, with no ambient inputs."""
        _text(source_key, "source_key")
        self.assert_held()
        row = self._connection.execute(
            "SELECT binding_digest, source_key_evidence, feature, signal, intent "
            "FROM source_opportunity_bindings WHERE source_key = ?",
            (source_key,),
        ).fetchone()
        if row is None:
            return None
        try:
            key_parts = decode_canonical(row[1])
            if (
                not isinstance(key_parts, tuple)
                or len(key_parts) != 14
                or key_parts[0] != "shadow.source-opportunity-key.v2"
            ):
                raise JournalError("source key evidence is malformed")
            from shadow.execution.opportunity import SourceOpportunityKey

            key = SourceOpportunityKey(*key_parts[1:])
            binding = SourceOpportunityBinding(
                key,
                self._decoded(row[2], FeatureSnapshot),
                self._decoded(row[3], Signal),
                self._decoded(row[4], OrderIntent),
            )
        except (SourceOpportunityError, ValueError, TypeError, JournalError) as exc:
            raise JournalError("source opportunity projection is malformed") from exc
        if binding.key.source_key != source_key or binding.evidence_digest != row[0]:
            raise JournalError("source opportunity projection conflicts with journal")
        return binding

    def record_terminal_admission(
        self, *, binding: SourceOpportunityBinding, decision: RiskDecision
    ) -> JournalAdmission:
        """Record the first decision for a binding, including terminal rejections.

        Admission evaluation remains outside this primitive.  Its only guarantee is
        that a reconnect cannot replace a recorded decision with changed evidence
        or turn an old rejection into a new opportunity.
        """
        if not isinstance(decision, RiskDecision):
            raise JournalError("decision must be a RiskDecision")
        persisted = self.bind_source_opportunity(binding)
        if decision.intent != persisted.intent:
            raise JournalError("decision intent does not match source binding")
        admission = JournalAdmission(
            persisted.key.source_key,
            persisted.evidence_digest,
            decision.intent.intent_identity,
            decision,
        )
        encoded = self._encoded(decision)
        self._transaction()
        try:
            existing = self._connection.execute(
                "SELECT binding_digest, intent_identity, decision FROM terminal_admissions "
                "WHERE source_key = ?",
                (admission.source_key,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (
                    admission.binding_digest,
                    admission.intent_identity,
                    encoded,
                ):
                    raise JournalError("terminal admission conflicts with first decision")
                self._commit()
                return admission
            self._connection.execute(
                "INSERT INTO terminal_admissions VALUES (?, ?, ?, ?)",
                (
                    admission.source_key,
                    admission.binding_digest,
                    admission.intent_identity,
                    encoded,
                ),
            )
            self._connection.execute(
                "INSERT INTO journal_events (occurred_at, kind, source_key, payload) "
                "VALUES (?, 'admission_recorded', ?, ?)",
                (decision.decision_time.isoformat(), admission.source_key, encoded),
            )
            self._commit()
        except (sqlite3.Error, JournalError):
            self._rollback()
            raise
        return admission

    def terminal_admission(self, source_key: str) -> JournalAdmission | None:
        _text(source_key, "source_key")
        self.assert_held()
        row = self._connection.execute(
            "SELECT binding_digest, intent_identity, decision FROM terminal_admissions "
            "WHERE source_key = ?",
            (source_key,),
        ).fetchone()
        if row is None:
            return None
        binding = self.source_binding(source_key)
        if binding is None:
            raise JournalError("admission has no source binding")
        try:
            admission = JournalAdmission(
                source_key,
                str(row[0]),
                str(row[1]),
                self._decoded(row[2], RiskDecision),
            )
        except (ValueError, JournalError) as exc:
            raise JournalError("terminal admission projection is malformed") from exc
        if (
            admission.binding_digest != binding.evidence_digest
            or admission.decision.intent != binding.intent
        ):
            raise JournalError("terminal admission projection conflicts with binding")
        return admission

    def verify_projections(self) -> None:
        """Fail closed unless append-only events reproduce current projections."""
        self.assert_held()
        events = self._connection.execute(
            "SELECT occurred_at, kind, source_key, payload FROM journal_events ORDER BY sequence"
        ).fetchall()
        bindings: set[str] = set()
        admissions: set[str] = set()
        for occurred_at, kind, source_key, payload in events:
            try:
                _utc(datetime.fromisoformat(str(occurred_at)), "event occurred_at")
                if kind == "source_bound":
                    binding = self.source_binding(str(source_key))
                    if binding is None:
                        raise JournalError("source binding event has no projection")
                    expected = self._encoded(binding.evidence_digest)
                    bindings.add(str(source_key))
                elif kind == "admission_recorded":
                    admission = self.terminal_admission(str(source_key))
                    if admission is None:
                        raise JournalError("admission event has no projection")
                    expected = self._encoded(admission.decision)
                    admissions.add(str(source_key))
                else:
                    raise JournalError("unknown journal event kind")
                if payload != expected:
                    raise JournalError("journal event differs from projection")
            except (TypeError, ValueError, JournalError) as exc:
                raise JournalError("journal projection replay failed") from exc
        counts = self._connection.execute(
            "SELECT (SELECT COUNT(*) FROM source_opportunity_bindings), "
            "(SELECT COUNT(*) FROM terminal_admissions)"
        ).fetchone()
        if counts != (len(bindings), len(admissions)):
            raise JournalError("journal projections have missing append-only events")

        if self.identity.schema_version == JOURNAL_SCHEMA_VERSION:
            from shadow.execution.btc_journal import BtcJournal

            BtcJournal(self)

    def commit_attempt(
        self,
        *,
        intent_identity: str,
        source_opportunity_id: str,
        client_order_id: str,
        client_order_full_digest: str,
        broker_trading_date: str,
        committed_at: datetime,
        dispatch_deadline: datetime,
        request: SubmitRequest,
        risk_decision: RiskDecision,
        account: BrokerAccount,
        clock: BrokerClock,
        asset: BrokerAsset,
        snapshot: BrokerSnapshot,
    ) -> CommittedAttempt:
        """Durably spend an intent before any provider POST is allowed."""
        candidate = CommittedAttempt(
            intent_identity,
            source_opportunity_id,
            client_order_id,
            client_order_full_digest,
            broker_trading_date,
            1,
            committed_at,
            dispatch_deadline,
            request,
            risk_decision,
            account,
            clock,
            asset,
            snapshot,
            None,
            None,
        )
        if request.client_id != client_order_id:
            raise JournalError("request/client identity mismatch")
        self._transaction()
        try:
            existing = self._connection.execute(
                "SELECT intent_identity FROM committed_attempts WHERE intent_identity = ?",
                (intent_identity,),
            ).fetchone()
            if existing is not None:
                raise JournalError("logical intent already has a committed attempt")
            sequence = self._connection.execute(
                "SELECT COALESCE(MAX(attempt_sequence), 0) + 1 FROM committed_attempts"
            ).fetchone()
            assert sequence is not None
            candidate = CommittedAttempt(
                intent_identity,
                source_opportunity_id,
                client_order_id,
                client_order_full_digest,
                broker_trading_date,
                sequence[0],
                committed_at,
                dispatch_deadline,
                request,
                risk_decision,
                account,
                clock,
                asset,
                snapshot,
                None,
                None,
            )
            self._connection.execute(
                "INSERT INTO committed_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (
                    candidate.intent_identity,
                    candidate.source_opportunity_id,
                    candidate.client_order_id,
                    candidate.client_order_full_digest,
                    candidate.broker_trading_date,
                    candidate.attempt_sequence,
                    candidate.committed_at.isoformat(),
                    candidate.dispatch_deadline.isoformat(),
                    self._encoded(candidate.request),
                    self._encoded(candidate.risk_decision),
                    self._encoded(candidate.account),
                    self._encoded(candidate.clock),
                    self._encoded(candidate.asset),
                    self._encoded(candidate.snapshot),
                ),
            )
            self._commit()
        except (sqlite3.Error, JournalError):
            self._rollback()
            raise
        return candidate

    def attempt_for_intent(self, intent_identity: str) -> CommittedAttempt | None:
        _text(intent_identity, "intent_identity")
        self.assert_held()
        row = self._connection.execute(
            "SELECT * FROM committed_attempts WHERE intent_identity = ?", (intent_identity,)
        ).fetchone()
        return None if row is None else self._attempt_from_row(row)

    def committed_attempts(self) -> tuple[CommittedAttempt, ...]:
        self.assert_held()
        rows = self._connection.execute(
            "SELECT * FROM committed_attempts ORDER BY attempt_sequence"
        ).fetchall()
        return tuple(self._attempt_from_row(row) for row in rows)

    def _attempt_from_row(self, row: tuple[object, ...]) -> CommittedAttempt:
        try:
            committed_at = datetime.fromisoformat(str(row[6]))
            deadline = datetime.fromisoformat(str(row[7]))
        except ValueError as exc:
            raise JournalError("attempt timestamp is malformed") from exc
        submission = None if row[14] is None else self._decoded(row[14], SubmissionResult)
        reconciliation = None if row[15] is None else self._decoded(row[15], BrokerSnapshot)
        if isinstance(row[5], bool) or not isinstance(row[5], int):
            raise JournalError("attempt sequence is malformed")
        return CommittedAttempt(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            row[5],
            committed_at,
            deadline,
            self._decoded(row[8], SubmitRequest),
            self._decoded(row[9], RiskDecision),
            self._decoded(row[10], BrokerAccount),
            self._decoded(row[11], BrokerClock),
            self._decoded(row[12], BrokerAsset),
            self._decoded(row[13], BrokerSnapshot),
            submission,
            reconciliation,
        )

    def persist_submission(self, *, intent_identity: str, result: SubmissionResult) -> None:
        self._transaction()
        try:
            attempt = self.attempt_for_intent(intent_identity)
            if attempt is None:
                raise JournalError("cannot persist result for missing attempt")
            if attempt.submission is not None:
                raise JournalError("submission result is immutable")
            if attempt.request != result.request:
                raise JournalError("submission result request mismatch")
            self._connection.execute(
                "UPDATE committed_attempts SET submission = ? WHERE intent_identity = ?",
                (self._encoded(result), intent_identity),
            )
            self._commit()
        except (sqlite3.Error, JournalError):
            self._rollback()
            raise

    def persist_reconciliation(self, *, intent_identity: str, snapshot: BrokerSnapshot) -> None:
        self._transaction()
        try:
            attempt = self.attempt_for_intent(intent_identity)
            if attempt is None:
                raise JournalError("cannot persist reconciliation for missing attempt")
            if attempt.reconciliation is not None:
                raise JournalError("reconciliation evidence is immutable")
            self._connection.execute(
                "UPDATE committed_attempts SET reconciliation = ? WHERE intent_identity = ?",
                (self._encoded(snapshot), intent_identity),
            )
            self._commit()
        except (sqlite3.Error, JournalError):
            self._rollback()
            raise

    def record_halt(
        self, *, occurred_at: datetime, reason: str, intent_identity: str | None = None
    ) -> None:
        _utc(occurred_at, "occurred_at")
        _text(reason, "reason")
        if intent_identity is not None:
            _text(intent_identity, "intent_identity")
        self._transaction()
        try:
            self._connection.execute(
                "INSERT INTO journal_halts (occurred_at, reason, intent_identity) VALUES (?, ?, ?)",
                (occurred_at.isoformat(), reason, intent_identity),
            )
            self._commit()
        except sqlite3.Error as exc:
            self._rollback()
            raise JournalError("unable to persist halt") from exc

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._connection.close()

    def __enter__(self) -> ExecutionJournal:
        self.assert_held()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()
