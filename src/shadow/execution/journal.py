"""P5A.2A local journal identity: explicit create/reopen, no event authority."""

from __future__ import annotations

import os
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from shadow.execution.journal_codec import CODEC_VERSION
from shadow.execution.ownership import AccountOwner, OwnershipError
from shadow.risk.models import OrderTarget

JOURNAL_SCHEMA_VERSION = "shadow.execution.journal.v1"


class JournalError(RuntimeError):
    """The local durable journal cannot establish its immutable identity."""


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
            ("table", "journal_metadata"),
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

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._connection.close()

    def __enter__(self) -> ExecutionJournal:
        self.assert_held()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()
