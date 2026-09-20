# ruff: noqa: E501
"""P5A.2A local journal identity: explicit create/reopen, no event authority."""

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
from shadow.execution.ownership import AccountOwner, OwnershipError
from shadow.risk.models import OrderTarget, RiskDecision

JOURNAL_SCHEMA_VERSION = "shadow.execution.journal.v2"
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
        return cls(candidate, identity, connection, owner)

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
        return cls(candidate, identity, connection, owner)

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
            connection.execute("COMMIT")
        except sqlite3.Error:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _read_identity(connection: sqlite3.Connection) -> JournalIdentity:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise JournalError("journal integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise JournalError("journal foreign key check failed")
        expected_objects = {
            ("table", "committed_attempts"),
            ("table", "journal_metadata"),
            ("table", "journal_halts"),
            ("trigger", "committed_attempts_transition_only"),
            ("trigger", "journal_metadata_immutable_delete"),
            ("trigger", "journal_metadata_immutable_update"),
        }
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
        if row[4] != JOURNAL_SCHEMA_VERSION or row[5] != CODEC_VERSION:
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
