"""Explicit asynchronous native-file I/O; metadata values do not own a client."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import random
import re
import stat as stat_module
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, Coroutine

import aiohttp
from aiohttp import ContentTypeError

from ._context import invoke_async as _invoke_async
from .errors import RemoteError, SessionClosed, error_from_wire

FILE_VERSION = 1
FILE_CONTENT_TYPE = "application/vnd.comani.file-v1"
FILE_METADATA_LIMIT = 64 * 1024
FILE_CHUNK_SIZE = 1024 * 1024


async def _io(function, *args, _cancel_cleanup=None, **kwargs):
    """Drain disk work and resource cleanup even after repeated cancellation."""
    loop = asyncio.get_running_loop()

    async def drain(future):
        cancelled = False
        while True:
            try:
                return await asyncio.shield(future), cancelled
            except asyncio.CancelledError:
                if future.cancelled():
                    raise
                cancelled = True

    result, cancelled = await drain(loop.run_in_executor(None, partial(function, *args, **kwargs)))
    if cancelled:
        if _cancel_cleanup is not None:
            await drain(loop.run_in_executor(None, _cancel_cleanup, result))
        raise asyncio.CancelledError
    return result


def _revision(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


@dataclass(frozen=True)
class FileRoot:
    id: str
    path: str
    directory: str | None
    category: str | None
    priority: int


@dataclass(frozen=True)
class FileDescriptor:
    """Saved location and expected bytes, without a session or root grant.

    The root path is only matched against the receiving daemon's current
    configured roots. It never authorizes opening an arbitrary server path.
    """

    root_path: str
    relative_path: str
    size: int
    sha256: str
    directory: str | None = None
    category: str | None = None
    version: int = 1

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1:
            raise ValueError("Unsupported file descriptor version")
        if (self.directory is None) == (self.category is None):
            raise ValueError("A file descriptor selects one native directory or model category")
        if self.directory is not None and self.directory not in {"input", "temp", "output"}:
            raise ValueError("Unknown native data directory")
        if self.category is not None and (not isinstance(self.category, str) or not self.category):
            raise ValueError("Expected a native model category")
        if any(not isinstance(value, str) or not value
               for value in (self.root_path, self.relative_path)):
            raise ValueError("A file descriptor requires a root path and relative file path")
        if type(self.size) is not int or self.size < 0:
            raise ValueError("A file descriptor requires a nonnegative file size")
        if not isinstance(self.sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise ValueError("A file descriptor requires a SHA256 checksum")


def _descriptor(value):
    if isinstance(value, FileDescriptor):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("descriptor must be a FileDescriptor or its saved mapping")
    return FileDescriptor(**value)


@dataclass(frozen=True)
class RemoteFile:
    root: FileRoot
    relative_path: str
    native_name: str
    size: int
    sha256: str | None
    revision: tuple[int, ...]

    @property
    def name(self):
        """The root-relative path; use native_name as a native node input."""
        return self.relative_path

    def describe(self) -> FileDescriptor:
        """Export a checksum-bearing locator; save with ``dataclasses.asdict``.

        This is local metadata conversion, not I/O. Obtain a missing checksum
        with ``await files.stat(file, checksum=True)`` before saving it.
        """
        if self.sha256 is None:
            raise ValueError("File SHA256 is unknown; call files.stat(file, checksum=True) first")
        return FileDescriptor(root_path=self.root.path, relative_path=self.relative_path,
                              size=self.size, sha256=self.sha256,
                              directory=self.root.directory, category=self.root.category)


@dataclass(frozen=True)
class DownloadedFile:
    local_path: Path
    size: int
    sha256: str
    remote: RemoteFile


def _remote(value):
    return RemoteFile(FileRoot(**value["root"]), value["relative_path"],
                      value["native_name"], value["size"], value["sha256"],
                      tuple(value["revision"]))


def _selector(value=None, *, directory=None, root=None):
    if isinstance(value, RemoteFile):
        if directory is not None or root is not None:
            raise TypeError("RemoteFile already selects its native root")
        return {"root_id": value.root.id, "path": value.relative_path,
                "revision": list(value.revision), "sha256": value.sha256}
    if directory is not None and root is not None:
        raise TypeError("Select directory or root, not both")
    if directory is not None and directory not in ("input", "temp", "output"):
        raise ValueError("directory must be 'input', 'temp' or 'output'")
    if root is not None and not isinstance(root, FileRoot):
        raise TypeError("root must be a FileRoot returned by files.roots()")
    result = {"root_id": root.id} if root is not None else {"directory": directory or "input"}
    if value is not None:
        if not isinstance(value, str):
            raise TypeError("Remote paths must be explicit root-relative strings or RemoteFile values")
        result["path"] = value
    return result


async def _json_response(response):
    try:
        value = await response.json()
    except (ValueError, TypeError, ContentTypeError):
        response.raise_for_status()
        raise RemoteError("Daemon returned invalid file metadata") from None
    if isinstance(value, dict) and "error" in value:
        raise error_from_wire(value["error"])
    response.raise_for_status()
    if not isinstance(value, dict):
        raise RemoteError("Daemon returned invalid file metadata")
    return value.get("result", value)


class Files:
    """A static file-I/O proxy that shares its Client's explicit lifetime.

    Holding this proxy keeps the client alive. Client does not cache the proxy,
    so there is no ownership cycle. Returned metadata never retains either.
    """

    def __init__(self, client):
        self._client = client

    async def _run(self, operation, *args, _stream=False, **kwargs):
        client = self._client
        await client.start()
        handshake = client.management_handshake or client.handshake
        capabilities = handshake.get("capabilities", {})
        if (not isinstance(capabilities, dict) or type(capabilities.get("files")) is not int
                or capabilities["files"] != FILE_VERSION):
            raise RemoteError("This daemon does not support explicit file I/O")
        if _stream:
            # Managed file I/O uses its independent daemon epoch even after a
            # compute attach replaces Client.handshake with the native schema.
            options = None
            version = handshake.get("capabilities", {}).get("files_resume")
            if type(version) is int and version == 1:
                options = {"epoch": handshake.get("file_transfer_epoch"),
                           "limits": handshake.get("file_limits", {})}
            args = (*args, options)
        task = client._task(operation(client.transport, client.session_id, *args, **kwargs))
        client._file_tasks.add(task)
        task.add_done_callback(client._file_tasks.discard, context=contextvars.Context())
        try:
            return await task
        except asyncio.CancelledError:
            if client.closed or client._closing:
                raise SessionClosed("The client closed during file I/O") from None
            raise

    async def roots(self, *, directory=None, category=None) -> tuple[FileRoot, ...]:
        async def operation(transport, session):
            values = await transport.request("file_roots", session_id=session,
                                             directory=directory, category=category)
            return tuple(FileRoot(**value) for value in values)
        return await self._run(operation)

    async def from_ui(self, ui, *, node_id=None, key=None) -> tuple[RemoteFile, ...]:
        """Resolve every standard Comfy filename/subfolder/type descriptor.

        Pass a call's native UI mapping. Optional node_id and key select one
        node and one UI field; the returned tuple preserves descriptor order.
        This performs file metadata I/O and never fetches computation outputs.
        """
        if not isinstance(ui, Mapping):
            raise TypeError("ui must be a native Comfy UI mapping")
        selected = ui
        if node_id is not None:
            selected = selected[str(node_id)]
        if key is not None:
            if node_id is not None:
                selected = selected[key]
            else:
                selected = [node[key] for node in selected.values()
                            if isinstance(node, Mapping) and key in node]
        descriptors = []

        def visit(value):
            if isinstance(value, Mapping):
                if "filename" in value and "type" in value:
                    filename, subfolder, directory = (value["filename"],
                                                       value.get("subfolder", ""), value["type"])
                    if (not isinstance(filename, str) or not filename
                            or not isinstance(subfolder, str)
                            or not isinstance(directory, str)
                            or directory not in {"input", "temp", "output"}):
                        raise ValueError("Invalid native Comfy file descriptor")
                    descriptors.append((f"{subfolder}/{filename}" if subfolder else filename,
                                        directory))
                else:
                    for child in value.values():
                        visit(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    visit(child)

        visit(selected)
        return tuple([await self.stat(path, directory=directory)
                      for path, directory in descriptors])

    async def stat(self, remote_file, *, directory=None, root=None, checksum=False) -> RemoteFile:
        selection = _selector(remote_file, directory=directory, root=root)

        async def operation(transport, session):
            return _remote(await transport.request("file_stat", session_id=session,
                                                    checksum=checksum, **selection))
        return await self._run(operation)

    async def resolve(self, descriptor: FileDescriptor | Mapping) -> RemoteFile:
        """Relocate saved metadata on the daemon and verify its expected bytes.

        This always requests a complete SHA256 scan. A changed or no-longer
        configured root, missing file, or content mismatch fails explicitly.
        """
        saved = asdict(_descriptor(descriptor))

        async def operation(transport, session):
            return _remote(await transport.request("file_stat", session_id=session,
                                                    checksum=True, descriptor=saved))
        return await self._run(operation)

    async def remove(self, remote_file, *, directory=None, root=None, missing_ok=False) -> None:
        selection = _selector(remote_file, directory=directory, root=root)

        async def operation(transport, session):
            await transport.request("file_remove", session_id=session,
                                    missing_ok=missing_ok, **selection)
        await self._run(operation)

    async def upload(self, local_path, *, directory=None, root=None, path=None,
                     recovery_timeout=None) -> RemoteFile:
        """Stream once normally; reconnect by the daemon-confirmed byte offset.

        recovery_timeout defaults to the daemon's file idle window. Recovery
        rereads the confirmed local prefix to rebuild SHA256. Explicit task
        cancellation stops the transfer and cleans up its own partial file.
        Older daemons retain their single-attempt, fail-clean behavior.
        """
        source = Path(local_path)
        if path is None:
            suffix = source.suffix
            path = uuid.uuid4().hex + (suffix if re.fullmatch(r"\.[\w-]{1,32}", suffix) else "")
        selection = _selector(path, directory=directory, root=root)
        return await self._run(_upload_http, source, selection, recovery_timeout, _stream=True)

    async def download(self, remote_file, local_path, *, directory=None, root=None,
                       overwrite=False, recovery_timeout=None) -> DownloadedFile:
        """Download atomically, resuming only the same source revision.

        recovery_timeout defaults to the daemon's file idle window. A resumed
        server stream may reread its prefix to compute the whole-file SHA256.
        """
        if type(overwrite) is not bool:
            raise TypeError("overwrite must be bool")
        selection = _selector(remote_file, directory=directory, root=root)
        return await self._run(_download_http, selection, Path(local_path).absolute(), overwrite,
                               recovery_timeout, _stream=True)


def _connection(transport):
    connection = getattr(transport, "_file_connection", None)
    if not callable(connection):
        raise RemoteError("This transport does not support file streaming; use HTTPTransport",
                          details={"code": "UnsupportedTransport"})
    return connection()


async def _upload_http(transport, session_id, source, selection, recovery_timeout=None, options=None):
    _recovery_timeout(recovery_timeout)
    if options is not None:
        return await _upload_resumable(transport, session_id, source, selection,
                                       recovery_timeout, options)
    return await _upload_once(transport, session_id, source, selection)


async def _upload_once(transport, session_id, source, selection):
    session, base = _connection(transport)
    stream = await _io(source.open, "rb", _cancel_cleanup=lambda value: value.close())
    completed = {}
    try:
        before = os.fstat(stream.fileno())
        if not stat_module.S_ISREG(before.st_mode):
            raise ValueError("Upload source must be a regular local file")
        boundary = "comfier-" + uuid.uuid4().hex
        metadata = {"session_id": session_id, "size": before.st_size, **selection}
        encoded = json.dumps(metadata, ensure_ascii=True).encode()
        if len(encoded) > FILE_METADATA_LIMIT:
            raise ValueError("File metadata exceeds 64 KiB")

        def header(name, content_type):
            return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n"
                    f"Content-Type: {content_type}\r\n\r\n").encode("ascii")

        async def produce():
            digest, size = hashlib.sha256(), 0
            yield header("metadata", "application/json") + encoded + b"\r\n"
            yield header("file", "application/octet-stream")
            while chunk := await _io(stream.read, FILE_CHUNK_SIZE):
                size += len(chunk)
                if size > before.st_size:
                    raise OSError("Local upload source grew during transfer")
                digest.update(chunk)
                yield chunk
            if (size != before.st_size or _revision(os.fstat(stream.fileno())) != _revision(before)
                    or _revision(await _io(source.stat)) != _revision(before)):
                raise OSError("Local upload source changed during transfer")
            completed["sha256"] = digest.hexdigest()
            footer = json.dumps({"size": size, "sha256": completed["sha256"]}).encode()
            yield (b"\r\n" + header("verified", "application/json") + footer
                   + f"\r\n--{boundary}--\r\n".encode("ascii"))

        async def body():
            completed["producer"] = asyncio.current_task()
            try:
                async for part in produce():
                    yield part
            except BaseException as error:
                completed["error"] = error
                raise

        try:
            async with session.post(base + "/files/upload", data=body(),
                                    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}) as response:
                info = _remote(await _json_response(response))
        except Exception:
            if isinstance(completed.get("error"), OSError):
                raise completed["error"] from None
            raise
        if completed.get("sha256") != info.sha256 or info.size != before.st_size:
            raise RemoteError("Uploaded file metadata does not match the local source")
        return info
    finally:
        producer = completed.get("producer")
        if producer is not None and producer is not asyncio.current_task() and not producer.done():
            producer.cancel()
            while not producer.done():
                try:
                    await asyncio.shield(producer)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
        stream.close()


async def _download_http(transport, session_id, selection, target, overwrite,
                         recovery_timeout=None, options=None):
    _recovery_timeout(recovery_timeout)
    if options is not None:
        return await _download_resumable(transport, session_id, selection, target, overwrite,
                                         recovery_timeout, options)
    return await _download_once(transport, session_id, selection, target, overwrite)


async def _download_once(transport, session_id, selection, target, overwrite):
    session, base = _connection(transport)
    if not overwrite and await _io(target.exists):
        raise FileExistsError(str(target))
    temporary = stream = None
    try:
        async with session.post(base + "/files/download",
                                json={"session_id": session_id, **selection}) as response:
            if response.status != 200 or response.content_type != FILE_CONTENT_TYPE:
                await _json_response(response)
                raise RemoteError("Daemon returned an invalid file stream")
            length = int.from_bytes(await response.content.readexactly(4), "big")
            if not 0 < length <= FILE_METADATA_LIMIT:
                raise RemoteError("Daemon returned oversized file metadata")
            info = _remote(json.loads(await response.content.readexactly(length)))
            if type(info.size) is not int or info.size < 0:
                raise RemoteError("Daemon returned an invalid file size")
            await _io(target.parent.mkdir, parents=True, exist_ok=True)
            def discard_created(value):
                descriptor, name = value
                os.close(descriptor)
                Path(name).unlink(missing_ok=True)

            descriptor, name = await _io(tempfile.mkstemp, prefix=".comani-", suffix=".part",
                                         dir=target.parent, _cancel_cleanup=discard_created)
            temporary, stream = Path(name), os.fdopen(descriptor, "wb")
            digest, remaining = hashlib.sha256(), info.size
            while remaining:
                chunk = await response.content.read(min(FILE_CHUNK_SIZE, remaining))
                if not chunk:
                    raise RemoteError("Daemon file stream ended before the declared size")
                if await _io(stream.write, chunk) != len(chunk):
                    raise OSError("Incomplete local download write")
                digest.update(chunk)
                remaining -= len(chunk)
            length = int.from_bytes(await response.content.readexactly(4), "big")
            if not 0 < length <= FILE_METADATA_LIMIT:
                raise RemoteError("Daemon returned an invalid verification footer")
            verified = json.loads(await response.content.readexactly(length))
            if (not isinstance(verified, dict) or verified.get("size") != info.size
                    or digest.hexdigest() != verified.get("sha256")
                    or (selection.get("sha256") is not None
                        and digest.hexdigest() != selection["sha256"])):
                raise RemoteError("Downloaded file SHA256 does not match")
            if await response.content.read(1):
                raise RemoteError("Unexpected bytes after file verification")
            info = replace(info, sha256=digest.hexdigest())
            await _io(stream.flush)
            await _io(os.fsync, stream.fileno())
            stream.close()
            stream = None
            if overwrite:
                await _io(os.replace, temporary, target)
            else:
                await _io(os.link, temporary, target)
                temporary.unlink()
            temporary = None
            return DownloadedFile(target, info.size, info.sha256, info)
    finally:
        try:
            if stream is not None:
                stream.close()
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _recovery_timeout(value):
    if value is not None and (not isinstance(value, (float, int)) or isinstance(value, bool)
            or not 0 < value < float("inf")):
        raise ValueError("recovery_timeout must be positive and finite")


def _network_error(error):
    if isinstance(error, (aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError,
                          aiohttp.ServerFingerprintMismatch)):
        return False
    if isinstance(error, aiohttp.ClientResponseError):
        return error.status in {408, 429} or 500 <= error.status < 600
    return isinstance(error, (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError,
                              asyncio.IncompleteReadError, TimeoutError))


class _FileTransfer:
    def __init__(self, transport, session_id, options, timeout):
        self.http, self.base = _connection(transport)
        self.identity = {"session_id": session_id, "epoch": options.get("epoch"),
                         "transfer_id": uuid.uuid4().hex}
        if not isinstance(self.identity["epoch"], str):
            raise RemoteError("Daemon advertised resumable files without a file transfer epoch")
        self.timeout = (options.get("limits", {}).get("transfer_idle_timeout", 3600.0)
                        if timeout is None else timeout)
        _recovery_timeout(self.timeout)
        self.since, self.delay = None, 0.05

    def headers(self):
        return {"X-Comani-Session": self.identity["session_id"],
                "X-Comani-Epoch": self.identity["epoch"]}

    async def pause(self, error, *, admission=False):
        busy = (isinstance(error, RemoteError) and isinstance(error.details, dict)
                and error.details.get("code") == "FileTransferBusy")
        if not _network_error(error) and not (admission and busy):
            raise error
        now = time.monotonic()
        self.since = now if self.since is None else self.since
        if now - self.since >= self.timeout:
            raise error
        await asyncio.sleep(min(self.delay * random.uniform(0.8, 1.2),
                                self.timeout - (now - self.since)))
        self.delay = min(2.0, self.delay * 2)

    def connected(self):
        self.since, self.delay = None, 0.05

    def remaining(self):
        return (self.timeout if self.since is None else
                max(0.001, self.timeout - (time.monotonic() - self.since)))

    async def request(self, operation, *, admission=False, **params):
        if operation.endswith("/start") and "window" not in params:
            window = await self.request("window")
            params = {**params, "window": window["window"]}
        while True:
            try:
                async with self.http.post(self.base + "/files/transfers/" + operation,
                                          json={**self.identity, **params},
                                          timeout=aiohttp.ClientTimeout(total=min(30.0, self.remaining()))) as response:
                    if response.status in {408, 429} or response.status >= 500:
                        response.raise_for_status()
                    result = await _json_response(response)
                if operation != "status":
                    self.connected()
                return result
            except Exception as error:
                await self.pause(error, admission=admission)

    async def cancel(self):
        # A cancelled caller still owns cleanup. Drain this bounded request even
        # if it calls Task.cancel() repeatedly; an offline daemon uses its TTL.
        async def cleanup():
            try:
                async with self.http.post(self.base + "/files/transfers/cancel", json=self.identity,
                                          timeout=aiohttp.ClientTimeout(total=2)) as response:
                    await response.read()
            except Exception:
                pass

        pending = asyncio.create_task(cleanup())
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                continue
        pending.result()


def _source_unchanged(stream, source, before):
    if (_revision(os.fstat(stream.fileno())) != _revision(before)
            or _revision(source.stat()) != _revision(before)):
        raise OSError("Local upload source changed during transfer")


def _prefix(stream, source, before, offset):
    """Only recovery rereads the confirmed prefix; ordinary uploads read once."""
    _source_unchanged(stream, source, before)
    stream.seek(0)
    digest, remaining = hashlib.sha256(), offset
    while remaining:
        chunk = stream.read(min(FILE_CHUNK_SIZE, remaining))
        if not chunk:
            raise OSError("Local upload source changed during recovery")
        digest.update(chunk)
        remaining -= len(chunk)
    _source_unchanged(stream, source, before)
    return digest


async def _producer_done(state, *, cancel):
    producer = state.get("producer")
    if producer is None or producer is asyncio.current_task():
        return
    if cancel and not producer.done():
        producer.cancel()
    cancelled = False
    while not producer.done():
        try:
            await asyncio.shield(producer)
        except asyncio.CancelledError:
            if not producer.cancelled():
                cancelled = True
        except Exception:
            break
    if cancelled:
        raise asyncio.CancelledError
    error = state.get("error")
    if not cancel and error is not None:
        if not (state.get("verified") and isinstance(error, (asyncio.CancelledError, GeneratorExit))):
            raise error


async def _upload_resumable(transport, session_id, source, selection, timeout, options):
    transfer = _FileTransfer(transport, session_id, options, timeout)
    stream = await _io(source.open, "rb", _cancel_cleanup=lambda value: value.close())
    state, success = {}, False
    try:
        before = os.fstat(stream.fileno())
        if not stat_module.S_ISREG(before.st_mode):
            raise ValueError("Upload source must be a regular local file")
        status = await transfer.request("upload/start", admission=True, size=before.st_size, **selection)
        offset, acknowledged, digest = 0, 0, hashlib.sha256()
        while True:
            if status.get("result") is not None:
                # This branch can only follow a commit/query on this identity.
                info = _remote(status["result"])
                if info.size != before.st_size or info.sha256 != digest.hexdigest():
                    raise RemoteError("Uploaded file metadata does not match the local source")
                success = True
                return info
            confirmed = status.get("offset")
            if type(confirmed) is not int or not 0 <= confirmed <= before.st_size:
                raise RemoteError("Daemon returned an invalid confirmed upload offset")
            if confirmed > acknowledged:
                acknowledged = confirmed
                transfer.connected()
            if confirmed != offset:
                digest = await _io(_prefix, stream, source, before, confirmed)
                offset = confirmed
            await _io(_source_unchanged, stream, source, before)
            state = {}

            async def body():
                nonlocal offset
                state["producer"] = asyncio.current_task()
                try:
                    while offset < before.st_size:
                        chunk = await _io(stream.read, min(FILE_CHUNK_SIZE, before.st_size - offset))
                        if not chunk:
                            raise OSError("Local upload source changed during transfer")
                        if offset + len(chunk) > before.st_size:
                            raise OSError("Local upload source grew during transfer")
                        digest.update(chunk)
                        offset += len(chunk)
                        if offset == before.st_size:
                            # Content-Length lets the peer answer immediately
                            # after the last byte. Finish revision validation
                            # before yielding it, while the producer still owns
                            # its source worker.
                            await _io(_source_unchanged, stream, source, before)
                            state["verified"] = True
                        yield chunk
                except BaseException as error:
                    state["error"] = error
                    raise

            try:
                if offset < before.st_size:
                    headers = {**transfer.headers(), "Content-Type": "application/octet-stream",
                               "X-Comani-Offset": str(offset),
                               "Content-Length": str(before.st_size - offset)}
                    url = transfer.base + "/files/transfers/" + transfer.identity["transfer_id"] + "/append"
                    async with transfer.http.post(url, data=body(), headers=headers,
                                                  timeout=aiohttp.ClientTimeout(
                                                      total=getattr(transport, "timeout", 3600),
                                                      sock_connect=min(30.0, transfer.remaining()),
                                                      sock_read=transfer.remaining())) as response:
                        if response.status in {408, 429} or response.status >= 500:
                            response.raise_for_status()
                        status = await _json_response(response)
                    await _producer_done(state, cancel=False)
                    if status.get("offset") != before.st_size:
                        raise RemoteError("Daemon acknowledged an incomplete upload")
                await _io(_source_unchanged, stream, source, before)
                status = await transfer.request("commit", sha256=digest.hexdigest())
            except Exception as error:
                await _producer_done(state, cancel=True)
                local = state.get("error")
                if isinstance(local, OSError):
                    raise local from None
                await transfer.pause(error)
                status = await transfer.request("status")
                # A producer may have hashed bytes still in a dead socket's
                # buffer. Always rebuild from the daemon's confirmed prefix.
                if status.get("result") is None:
                    confirmed = status.get("offset")
                    if type(confirmed) is not int or not 0 <= confirmed <= before.st_size:
                        raise RemoteError("Daemon returned an invalid confirmed upload offset")
                    digest = await _io(_prefix, stream, source, before, confirmed)
                    offset = confirmed
    finally:
        try:
            await _producer_done(state, cancel=True)
        finally:
            stream.close()
            if not success:
                await transfer.cancel()


async def _download_resumable(transport, session_id, selection, target, overwrite, timeout, options):
    transfer = _FileTransfer(transport, session_id, options, timeout)
    if not overwrite and await _io(target.exists):
        raise FileExistsError(str(target))
    temporary = stream = None
    success = False
    try:
        status = await transfer.request("download/start", admission=True, **selection)
        info = _remote(status["metadata"])
        if type(info.size) is not int or info.size < 0:
            raise RemoteError("Daemon returned an invalid file size")
        await _io(target.parent.mkdir, parents=True, exist_ok=True)

        def discard(value):
            descriptor, name = value
            os.close(descriptor)
            Path(name).unlink(missing_ok=True)

        descriptor, name = await _io(tempfile.mkstemp, prefix=".comani-", suffix=".part",
                                     dir=target.parent, _cancel_cleanup=discard)
        temporary, stream = Path(name), os.fdopen(descriptor, "w+b", buffering=0)
        revision = _revision(os.fstat(stream.fileno()))
        digest, offset = hashlib.sha256(), 0
        while True:
            try:
                if offset < info.size or info.size == 0 and status.get("result") is None:
                    headers = transfer.headers()
                    if offset:
                        headers["Range"] = f"bytes={offset}-"
                    url = transfer.base + "/files/transfers/" + transfer.identity["transfer_id"] + "/data"
                    async with transfer.http.get(url, headers=headers,
                                                 timeout=aiohttp.ClientTimeout(total=None,
                                                     sock_connect=min(30.0, transfer.remaining()),
                                                     sock_read=transfer.remaining())) as response:
                        if response.status in {408, 429} or response.status >= 500:
                            response.raise_for_status()
                        if response.status not in {200, 206}:
                            await _json_response(response)
                        expected_range = f"bytes {offset}-{info.size - 1}/{info.size}"
                        if (response.status != (206 if offset else 200)
                                or response.content_type != "application/octet-stream"
                                or response.headers.get("X-Comani-Epoch") != transfer.identity["epoch"]
                                or response.headers.get("X-Comani-Offset") != str(offset)
                                or response.content_length != info.size - offset
                                or (offset and response.headers.get("Content-Range") != expected_range)
                                or (not offset and "Content-Range" in response.headers)):
                            raise RemoteError("Daemon returned an invalid file byte range")
                        while offset < info.size:
                            chunk = await response.content.read(min(FILE_CHUNK_SIZE, info.size - offset))
                            if not chunk:
                                raise aiohttp.ClientPayloadError("File stream ended before its declared size")
                            if (_revision(os.fstat(stream.fileno())) != revision
                                    or _revision(await _io(temporary.stat)) != revision):
                                raise OSError("Local download staging file changed")
                            if await _io(stream.write, chunk) != len(chunk):
                                raise OSError("Incomplete local download write")
                            revision = _revision(os.fstat(stream.fileno()))
                            digest.update(chunk)
                            offset += len(chunk)
                            transfer.connected()
                        if await response.content.read(1):
                            raise RemoteError("Unexpected bytes after the selected file range")
                    transfer.connected()
                status = await transfer.request("status")
                if status.get("result") is None:
                    status = await transfer.request("verify")
                verified = _remote(status["result"])
                if (verified.revision != info.revision or verified.relative_path != info.relative_path
                        or verified.root != info.root or verified.size != info.size
                        or verified.sha256 != digest.hexdigest()
                        or selection.get("sha256") is not None
                        and digest.hexdigest() != selection["sha256"]):
                    raise RemoteError("Downloaded file SHA256 or revision does not match")
                break
            except Exception as error:
                await transfer.pause(error)
                status = await transfer.request("status")
        await _io(stream.flush)
        await _io(os.fsync, stream.fileno())
        if (_revision(os.fstat(stream.fileno())) != revision
                or _revision(await _io(temporary.stat)) != revision):
            raise OSError("Local download staging file changed")
        stream.close()
        stream = None
        if overwrite:
            await _io(os.replace, temporary, target)
        else:
            await _io(os.link, temporary, target)
            temporary.unlink()
        temporary = None
        success = True
        return DownloadedFile(target, verified.size, verified.sha256, verified)
    finally:
        try:
            if stream is not None:
                stream.close()
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if not success:
                await transfer.cancel()


# Module-level current-Client file I/O.
def roots(*, directory=None, category=None) -> Coroutine[Any, Any, tuple[FileRoot, ...]]:
    return _invoke_async(('files', 'roots'), directory=directory, category=category)


def from_ui(ui, *, node_id=None, key=None) -> Coroutine[Any, Any, tuple[RemoteFile, ...]]:
    return _invoke_async(('files', 'from_ui'), ui, node_id=node_id, key=key)


def stat(remote_file, *, directory=None, root=None, checksum=False) -> Coroutine[Any, Any, RemoteFile]:
    return _invoke_async(('files', 'stat'), remote_file, directory=directory, root=root, checksum=checksum)


def resolve(descriptor: FileDescriptor | Mapping) -> Coroutine[Any, Any, RemoteFile]:
    return _invoke_async(('files', 'resolve'), descriptor)


def remove(remote_file, *, directory=None, root=None, missing_ok=False) -> Coroutine[Any, Any, None]:
    return _invoke_async(('files', 'remove'), remote_file, directory=directory, root=root, missing_ok=missing_ok)


def upload(local_path, *, directory=None, root=None, path=None, recovery_timeout=None) -> Coroutine[Any, Any, RemoteFile]:
    return _invoke_async(('files', 'upload'), local_path, directory=directory, root=root, path=path, recovery_timeout=recovery_timeout)


def download(remote_file, local_path, *, directory=None, root=None, overwrite=False, recovery_timeout=None) -> Coroutine[Any, Any, DownloadedFile]:
    return _invoke_async(('files', 'download'), remote_file, local_path, directory=directory, root=root, overwrite=overwrite, recovery_timeout=recovery_timeout)
