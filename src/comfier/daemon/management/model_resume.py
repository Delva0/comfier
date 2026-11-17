"""Private SHA-bound model partials, guarded across daemon/engine processes."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import time
from pathlib import Path

from ..files import _directory as _safe_directory
from .operations import ManagementError

RETENTION_SECONDS = 24 * 60 * 60


def _directory(path):
    # Creation must not walk a replaced/symlinked .comani ancestor.
    _safe_directory(path.parent, create=True)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    value = path.lstat()
    if (not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid()
            or value.st_mode & 0o077):
        raise ValueError("Model resume storage must be a private daemon-owned directory")


def _open_lock(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    value = os.fstat(fd)
    if not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid() or value.st_nlink != 1:
        os.close(fd)
        raise ValueError("Model resume lock must be an owned regular file")
    return fd


def _manifest(path):
    try:
        value = path.lstat()
        if not stat.S_ISREG(value.st_mode) or value.st_size > 16384:
            return None
        with path.open() as stream:
            result = json.load(stream)
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


class ResumeEntry:
    def __init__(self, cache, directory, fd, manifest):
        self.cache, self.directory, self.fd, self.manifest = cache, directory, fd, manifest
        self.target = directory / "payload.part"

    def touch(self):
        self.manifest["updated_at"] = time.time()
        temporary = self.directory / "manifest.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(self.manifest, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.directory / "manifest.json")

    def reset(self):
        # Called only while this entry's exclusive lease is held.
        for name in ("payload.part", "payload.part.aria2"):
            (self.directory / name).unlink(missing_ok=True)
        self.target.touch(mode=0o600, exist_ok=False)

    def close(self, *, discard=False):
        if self.fd is None:
            return
        try:
            if discard:
                # Serialize deletion with acquisition: a waiter must not open
                # the soon-to-be-unlinked lock inode and then write unlocked.
                with self.cache._guard():
                    shutil.rmtree(self.directory)
            else:
                self.touch()
        finally:
            # Do not LOCK_UN: an inherited aria2 descriptor must continue to
            # own this same flock after a killed Python parent disappears.
            os.close(self.fd)
            self.fd = None


class ModelResumeCache:
    def __init__(self, workspace):
        # Keep existing resumable bytes addressable after the public rename.
        self.path = Path(workspace) / ".comani" / "model-resume"

    @contextlib.contextmanager
    def _guard(self):
        import fcntl

        _directory(self.path)
        fd = _open_lock(self.path / ".lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _clean(self, *, expired_only):
        import fcntl

        removed = skipped = 0
        now = time.time()
        for directory in sorted(self.path.iterdir()):
            if not re.fullmatch(r"[0-9a-f]{64}", directory.name):
                continue
            if not stat.S_ISDIR(directory.lstat().st_mode):
                continue
            fd = _open_lock(directory / ".lock")
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    skipped += 1
                    continue
                manifest = _manifest(directory / "manifest.json")
                updated = (manifest or {}).get("updated_at")
                if expired_only and isinstance(updated, (int, float)) and now - updated < RETENTION_SECONDS:
                    continue
                shutil.rmtree(directory)
                removed += 1
            finally:
                os.close(fd)
        return {"removed": removed, "skipped_active": skipped}

    def clear(self):
        if os.name != "posix":
            return {"removed": 0, "skipped_active": 0}
        with self._guard():
            return self._clean(expired_only=False)

    def acquire(self, identity, relative_path, expected_sha256, transport):
        import fcntl

        # Public epoch-scoped FileRoot IDs are deliberately absent. Keeping
        # only destination facts and digest also excludes source URLs/tokens.
        facts = {"schema": 1, "sha256": expected_sha256.lower(),
                 "root_path": str(identity.root.path), "root_identity": list(identity.identity),
                 "relative_path": relative_path}
        key = hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self._guard():
            self._clean(expired_only=True)
            directory = self.path / key
            _directory(directory)
            fd = _open_lock(directory / ".lock")
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ManagementError("DestinationBusy", "Another process is acquiring this model partial") from None
                previous = _manifest(directory / "manifest.json")
                entry = ResumeEntry(self, directory, fd, {**facts, "transport": transport})
                if (previous is None or any(previous.get(name) != value for name, value in facts.items())
                        or previous.get("transport") != transport):
                    entry.reset()
                else:
                    try:
                        value = entry.target.lstat()
                        if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
                                or value.st_nlink != 1 or value.st_mode & 0o022):
                            raise ValueError("Model partial must be an owned exclusive regular file")
                    except FileNotFoundError:
                        entry.reset()
                    try:
                        value = (directory / "payload.part.aria2").lstat()
                    except FileNotFoundError:
                        pass
                    else:
                        if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
                                or value.st_nlink != 1 or value.st_mode & 0o022):
                            raise ValueError("Model partial control must be an owned exclusive regular file")
                entry.touch()
                return entry
            except BaseException:
                os.close(fd)
                raise
