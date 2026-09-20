"""Linux-local advisory ownership lock for one paper account.

The lock is an operational guard, not distributed coordination or hostile-process
security.  The deployment boundary is one host and a supported local filesystem.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path


class OwnershipError(RuntimeError):
    """Ownership cannot be acquired or is no longer trustworthy."""


def _text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OwnershipError(f"{field} must be a nonempty trimmed string")


def _local_directory(path: Path) -> Path:
    if sys.platform != "linux" or os.name != "posix" or not hasattr(fcntl, "flock"):
        raise OwnershipError("P5A.2A ownership supports Linux POSIX locks only")
    try:
        raw = path.absolute()
        if raw.is_symlink():
            raise OwnershipError("ownership directory must not be a symlink")
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise OwnershipError("ownership directory is unavailable") from exc
    if not resolved.is_dir():
        raise OwnershipError("ownership path must be a directory")
    if raw != resolved:
        raise OwnershipError("ownership path aliases are unsupported")
    return resolved


@dataclass(slots=True)
class AccountOwner:
    """A nonblocking lock retained for the entire local owner lifetime."""

    account_id: str
    _directory: Path
    _path: Path
    _fd: int
    _pid: int
    _device: int
    _inode: int
    _journal_path: Path | None = None
    _closed: bool = False

    @classmethod
    def acquire(cls, *, ownership_directory: Path, account_id: str) -> AccountOwner:
        _text(account_id, "account_id")
        directory = _local_directory(ownership_directory)
        name = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
        path = directory / f"shadow-paper-account-{name}.lock"
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
            descriptor = os.fstat(fd)
            path_stat = os.stat(path, follow_symlinks=False)
            if not stat.S_ISREG(descriptor.st_mode) or (
                descriptor.st_dev,
                descriptor.st_ino,
            ) != (path_stat.st_dev, path_stat.st_ino):
                raise OwnershipError("ownership lock artifact was replaced")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            journal_path = cls._read_journal_path(fd, account_id)
        except (OSError, OwnershipError) as exc:
            if "fd" in locals():
                os.close(fd)
            raise OwnershipError("paper account already owned or lock unavailable") from exc
        return cls(
            account_id,
            directory,
            path,
            fd,
            os.getpid(),
            descriptor.st_dev,
            descriptor.st_ino,
            journal_path,
        )

    @staticmethod
    def _read_journal_path(fd: int, account_id: str) -> Path | None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            data = os.read(fd, 4096)
        except OSError as exc:
            raise OwnershipError("unable to read ownership binding") from exc
        if not data:
            return None
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OwnershipError("ownership binding is malformed") from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {"account_id", "journal_path"}
            or payload["account_id"] != account_id
            or not isinstance(payload["journal_path"], str)
        ):
            raise OwnershipError("ownership binding is invalid")
        path = Path(payload["journal_path"])
        if not path.is_absolute():
            raise OwnershipError("ownership journal path is not absolute")
        return path

    @property
    def path(self) -> Path:
        return self._path

    def bind_journal_path(self, *, journal_path: Path) -> None:
        """Bind the account lock to exactly one journal path without granting authority."""
        self.assert_held(account_id=self.account_id)
        if not journal_path.is_absolute():
            raise OwnershipError("journal path must be absolute")
        if self._journal_path is not None:
            if self._journal_path != journal_path:
                raise OwnershipError("paper account is already bound to another journal")
            return
        payload = json.dumps(
            {"account_id": self.account_id, "journal_path": str(journal_path)},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.ftruncate(self._fd, 0)
            os.write(self._fd, payload)
            os.fsync(self._fd)
        except OSError as exc:
            raise OwnershipError("unable to persist ownership journal binding") from exc
        self._journal_path = journal_path

    def assert_held(self, *, account_id: str) -> None:
        _text(account_id, "account_id")
        if self._closed or self._pid != os.getpid() or self.account_id != account_id:
            raise OwnershipError("ownership is not valid in this process/account")
        try:
            current = os.stat(self._path, follow_symlinks=False)
        except OSError as exc:
            raise OwnershipError("ownership artifact disappeared") from exc
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (
            self._device,
            self._inode,
        ):
            raise OwnershipError("ownership artifact was replaced")
        if self._read_journal_path(self._fd, self.account_id) != self._journal_path:
            raise OwnershipError("ownership journal binding changed")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)

    def __enter__(self) -> AccountOwner:
        self.assert_held(account_id=self.account_id)
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()
