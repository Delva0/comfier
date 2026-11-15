"""Explicit asynchronous native-file I/O; metadata values do not own a client."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

from aiohttp import ContentTypeError

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

    async def _run(self, operation, *args, **kwargs):
        client = self._client
        await client.start()
        capabilities = client.handshake.get("capabilities", {})
        if (not isinstance(capabilities, dict) or type(capabilities.get("files")) is not int
                or capabilities["files"] != FILE_VERSION):
            raise RemoteError("This daemon does not support explicit file I/O")
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

    async def stat(self, remote_file, *, directory=None, root=None, checksum=False) -> RemoteFile:
        selection = _selector(remote_file, directory=directory, root=root)

        async def operation(transport, session):
            return _remote(await transport.request("file_stat", session_id=session,
                                                    checksum=checksum, **selection))
        return await self._run(operation)

    async def remove(self, remote_file, *, directory=None, root=None, missing_ok=False) -> None:
        selection = _selector(remote_file, directory=directory, root=root)

        async def operation(transport, session):
            await transport.request("file_remove", session_id=session,
                                    missing_ok=missing_ok, **selection)
        await self._run(operation)

    async def upload(self, local_path, *, directory=None, root=None, path=None) -> RemoteFile:
        source = Path(local_path)
        if path is None:
            suffix = source.suffix
            path = uuid.uuid4().hex + (suffix if re.fullmatch(r"\.[\w-]{1,32}", suffix) else "")
        selection = _selector(path, directory=directory, root=root)
        return await self._run(_upload_http, source, selection)

    async def download(self, remote_file, local_path, *, directory=None, root=None,
                       overwrite=False) -> DownloadedFile:
        if type(overwrite) is not bool:
            raise TypeError("overwrite must be bool")
        selection = _selector(remote_file, directory=directory, root=root)
        return await self._run(_download_http, selection, Path(local_path).absolute(), overwrite)


def _connection(transport):
    connection = getattr(transport, "_file_connection", None)
    if not callable(connection):
        raise RemoteError("This transport does not support file streaming; use HTTPTransport",
                          details={"code": "UnsupportedTransport"})
    return connection()


async def _upload_http(transport, session_id, source, selection):
    session, base = _connection(transport)
    stream = await _io(source.open, "rb", _cancel_cleanup=lambda value: value.close())
    completed = {}
    try:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Upload source must be a regular local file")
        boundary = "comani-" + uuid.uuid4().hex
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


async def _download_http(transport, session_id, selection, target, overwrite):
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
