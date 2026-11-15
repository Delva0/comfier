"""Bounded, same-daemon operation admission and observation.

Request handlers never own operation tasks. A cancelled observer or disconnected
HTTP request therefore cannot cancel an admitted command.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import hmac
import json
import secrets
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field


class ManagementError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


@dataclass
class Record:
    id: str
    identity: str
    namespace: str
    action: str
    created: float = field(default_factory=time.time)
    state: str = "accepted"
    started: float | None = None
    finished: float | None = None
    progress: dict = field(default_factory=dict)
    result: object = None
    error: dict | None = None
    task: asyncio.Task | None = None
    logs: deque = field(default_factory=deque)
    log_bytes: int = 0
    cursor: int = 0
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    result_bytes: int = 0
    capture_remaining: int = 2 * 1024 * 1024
    settled: bool = False


class OperationContext:
    def __init__(self, registry, record):
        self.registry, self.record = registry, record

    def log(self, text, stream="system"):
        self.registry.log(self.record, str(text), stream)

    def progress(self, **values):
        self.record.progress.update(values)
        self.record.changed.set()

    def partial(self, result):
        self.record.result = result


class OperationRegistry:
    def __init__(self, *, max_records=4096, max_active=64, max_running=8, window_seconds=300,
                 result_seconds=3600, log_bytes=256 * 1024,
                 max_result_bytes=128 * 1024 * 1024, max_log_bytes=32 * 1024 * 1024,
                 max_result_size=8 * 1024 * 1024, capture_bytes=2 * 1024 * 1024):
        self.epoch = uuid.uuid4().hex
        self.max_records, self.window_seconds = max_records, window_seconds
        self.result_seconds, self.log_limit = result_seconds, log_bytes
        self.max_active, self.max_running = max_active, max_running
        self.max_result_bytes, self.max_log_bytes = max_result_bytes, max_log_bytes
        if max_result_size < 1024 or max_result_bytes < max_result_size:
            raise ValueError("Result quota must reserve at least one result of at least 1024 bytes")
        self.max_result_size, self.capture_bytes = max_result_size, capture_bytes
        self._result_bytes = self._log_bytes = 0
        self._reserved_result_bytes = 0
        self._secret = secrets.token_bytes(32)
        self._records, self._keys = {}, {}
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(max_running)
        self._closed = False

    def window(self, identity):
        expires = time.time() + self.window_seconds
        body = json.dumps([self.epoch, str(identity), expires, secrets.token_hex(8)],
                          separators=(",", ":")).encode()
        token = base64.urlsafe_b64encode(body).decode().rstrip("=")
        signature = hmac.new(self._secret, token.encode(), hashlib.sha256).hexdigest()
        return {"daemon_epoch": self.epoch,
                "window": {"token": token + "." + signature, "expires_at": expires},
                "limits": {"submission_seconds": self.window_seconds,
                           "result_seconds": self.result_seconds,
                           "max_operations": self.max_records,
                           "max_active": self.max_active, "max_running": self.max_running,
                           "max_result_bytes": self.max_result_bytes, "max_log_bytes": self.max_log_bytes,
                           "max_result_size": self.max_result_size,
                           "capture_bytes_per_operation": self.capture_bytes,
                           "log_bytes_per_operation": self.log_limit}}

    def _window(self, token, identity):
        if isinstance(token, dict):
            token = token.get("token")
        try:
            body, signature = token.rsplit(".", 1)
            expected = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            epoch, owner, expires, _ = json.loads(base64.urlsafe_b64decode(
                body + "=" * (-len(body) % 4)))
            if epoch != self.epoch or owner != str(identity):
                raise ValueError
            return float(expires)
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
            raise ManagementError("OperationExpired", "Invalid submission window") from None

    def _prune(self):
        now = time.time()
        pinned = {value[1] for value in self._keys.values() if value[2] > now}
        for oid, rec in tuple(self._records.items()):
            if (rec.state in TERMINAL and rec.finished + self.result_seconds < now
                    and oid not in pinned):
                self._result_bytes -= rec.result_bytes
                self._log_bytes -= rec.log_bytes
                del self._records[oid]
        self._keys = {key: value for key, value in self._keys.items() if value[1] in self._records}

    async def begin(self, identity, namespace, action, params, key, daemon_epoch, window, run,
                    *, admission_check=None):
        if daemon_epoch != self.epoch:
            raise ManagementError("OperationExpired", "The management daemon changed")
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise ManagementError("InvalidArgument", "A bounded submission key is required")
        expires = self._window(window, identity)
        try:
            encoded = json.dumps([namespace, action, params], sort_keys=True,
                                 separators=(",", ":"), allow_nan=False).encode()
        except (ValueError, TypeError):
            raise ManagementError("InvalidArgument", "Management arguments must be JSON values")
        if len(encoded) > 1024 * 1024:
            raise ManagementError("InvalidArgument", "Management request exceeds 1 MiB")
        digest = hashlib.sha256(encoded).hexdigest()
        async with self._lock:
            self._prune()
            previous = self._keys.get((str(identity), key))
            if previous is not None:
                if previous[0] != digest:
                    raise ManagementError("SubmissionKeyConflict", "Submission key has other arguments")
                return self.snapshot(self._records[previous[1]])
            if expires <= time.time():
                raise ManagementError("OperationExpired", "Submission window expired; outcome is unknown")
            if admission_check is not None:
                admission_check()
            active = sum(rec.state not in TERMINAL for rec in self._records.values())
            resource = ("active" if active >= self.max_active else "receipts" if len(self._records) >= self.max_records
                        else "results" if self._result_bytes + self._reserved_result_bytes + self.max_result_size > self.max_result_bytes else None)
            if self._closed or resource:
                raise ManagementError("OperationCapacity", "Management retention/admission capacity reached; existing operations remain observable",
                                      {"resource": resource or "closing"})
            rec = Record(uuid.uuid4().hex, str(identity), namespace, action)
            rec.capture_remaining = self.capture_bytes
            self._records[rec.id] = rec
            self._reserved_result_bytes += self.max_result_size
            self._keys[str(identity), key] = digest, rec.id, expires
            rec.task = asyncio.create_task(self._execute(rec, run), name=f"management:{rec.id}")
            return self.snapshot(rec)

    async def _execute(self, rec, run):
        context = OperationContext(self, rec)
        try:
            rec.state = "queued"
            async with self._execution_slot(rec):
                rec.state, rec.started = "running", time.time()
                rec.changed.set()
                rec.result = await run(context)
                rec.state = "succeeded"
        except asyncio.CancelledError:
            rec.state = "cancelled"
            rec.error = {"code": "OperationCancelled", "message": "Explicitly cancelled; changes are not rolled back"}
        except Exception as exc:
            rec.state = "failed"
            code = "DiskFull" if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT} else getattr(exc, "code", "OperationFailed")
            rec.error = {"code": code,
                         "message": str(exc), "details": getattr(exc, "details", {})}
        finally:
            rec.finished = time.time()
            self._settle(rec)
            rec.changed.set()

    @asynccontextmanager
    async def _execution_slot(self, rec):
        if rec.namespace == "comfyui" and rec.action in {"stop", "restart"}:
            # Lifecycle control is outside both the compute queue and the
            # subprocess/download worker slots. The supervisor owns its lock.
            yield
        else:
            async with self._slots:
                yield

    def _settle(self, rec):
        if rec.settled:
            return
        encoded = json.dumps({"result": rec.result, "error": rec.error}, default=str).encode()
        if len(encoded) > self.max_result_size:
            completed_state = rec.state
            summary = {"completed_state": completed_state, "result_retained": False,
                       "changes_may_have_occurred": True}
            if isinstance(rec.result, dict):
                summary["partial_changes"] = rec.result.get("partial_changes", "unknown")
                if isinstance(rec.result.get("files"), list):
                    summary["completed_files"] = len(rec.result["files"])
            rec.state, rec.result = "failed", summary
            rec.error = {"code": "OperationResultTooLarge",
                         "message": "Execution finished but its result exceeded retention limits; inspect remote facts before another operation",
                         "details": {"completed_state": completed_state, "result_bytes": len(encoded),
                                     "limit": self.max_result_size}}
            encoded = json.dumps({"result": rec.result, "error": rec.error}).encode()
        rec.result_bytes, rec.settled = len(encoded), True
        self._reserved_result_bytes -= self.max_result_size
        self._result_bytes += rec.result_bytes

    def get(self, operation_id, daemon_epoch, identity):
        if daemon_epoch != self.epoch:
            raise ManagementError("OperationExpired", "The management daemon changed")
        self._prune()
        rec = self._records.get(operation_id)
        if rec is None or rec.identity != str(identity):
            raise ManagementError("OperationExpired", "Operation is unknown or no longer retained")
        return rec

    def snapshot(self, rec):
        return {"operation_id": rec.id, "daemon_epoch": self.epoch,
                "namespace": rec.namespace, "action": rec.action, "state": rec.state,
                "created_at": rec.created, "started_at": rec.started, "finished_at": rec.finished,
                "progress": dict(rec.progress), "result": rec.result, "error": rec.error,
                "log_start": rec.logs[0]["cursor"] - 1 if rec.logs else rec.cursor,
                "log_end": rec.cursor}

    def log(self, rec, value, stream):
        # Each chunk has its own cursor; retaining a bounded suffix never disguises a gap.
        for offset in range(0, len(value), 4096):
            part = value[offset:offset + 4096]
            rec.cursor += 1
            entry = {"cursor": rec.cursor, "time": time.time(), "stream": stream, "text": part}
            size = len(part.encode("utf-8"))
            rec.logs.append(entry)
            rec.log_bytes += size
            self._log_bytes += size
            while rec.logs and rec.log_bytes > self.log_limit:
                removed = len(rec.logs.popleft()["text"].encode("utf-8"))
                rec.log_bytes -= removed
                self._log_bytes -= removed
            if self._log_bytes > self.max_log_bytes:
                for older in self._records.values():
                    while older.logs and self._log_bytes > self.max_log_bytes:
                        removed = len(older.logs.popleft()["text"].encode("utf-8"))
                        older.log_bytes -= removed
                        self._log_bytes -= removed
                    if self._log_bytes <= self.max_log_bytes:
                        break
        rec.changed.set()

    async def logs(self, rec, *, after=0, follow=False, timeout=20, limit=256):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1024:
            raise ManagementError("InvalidArgument", "Invalid log cursor or limit")
        start = rec.logs[0]["cursor"] - 1 if rec.logs else rec.cursor
        if after < start:
            raise ManagementError("LogExpired", "Requested log prefix expired", {"log_start": start})
        if after > rec.cursor:
            raise ManagementError("InvalidArgument", "Log cursor is ahead of the operation")
        if follow and after == rec.cursor and rec.state not in TERMINAL:
            rec.changed.clear()
            try:
                await asyncio.wait_for(rec.changed.wait(), min(max(float(timeout), 0), 25))
            except TimeoutError:
                pass
        entries = [dict(item) for item in rec.logs if item["cursor"] > after][:limit]
        return {"entries": entries, "next_cursor": entries[-1]["cursor"] if entries else after,
                "log_start": rec.logs[0]["cursor"] - 1 if rec.logs else rec.cursor,
                "done": rec.state in TERMINAL}

    async def cancel(self, rec):
        if rec.state not in TERMINAL:
            if rec.state != "cancelling":
                rec.state = "cancelling"
                rec.task.cancel()
            # A task cancelled before its first instruction cannot run its own
            # finally block. Admission still needs an observable terminal fact.
            await asyncio.shield(asyncio.gather(rec.task, return_exceptions=True))
            if rec.finished is None:
                rec.state, rec.finished = "cancelled", time.time()
                rec.error = {"code": "OperationCancelled", "message": "Cancelled before execution"}
                self._settle(rec)
                rec.changed.set()
        return self.snapshot(rec)

    async def close(self):
        self._closed = True
        tasks = [rec.task for rec in self._records.values() if rec.state not in TERMINAL]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
