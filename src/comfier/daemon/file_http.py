"""The bounded file wire protocol; no background upload jobs or ownership GC."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import astuple, dataclass, field, replace

from aiohttp import web

from comfier.files import FILE_CHUNK_SIZE, FILE_CONTENT_TYPE, FILE_METADATA_LIMIT, _descriptor, _io

from .files import FileChangedError, Revision
from .objects import SessionClosed


def root_wire(store, root):
    roots = store.roots(directory=root.directory, category=root.category)
    return {"id": root.id, "path": str(root.path), "directory": root.directory,
            "category": root.category, "priority": roots.index(root)}


def metadata_wire(store, info):
    return {"root": root_wire(store, info.root), "relative_path": info.relative_path,
            "native_name": info.native_name, "size": info.size, "sha256": info.sha256,
            "revision": list(astuple(info.revision))}


def selection(store, params):
    if params.get("root_id") is not None:
        if params.get("directory") is not None:
            raise ValueError("Select root_id or directory, not both")
        root = store.root_by_id(params["root_id"])
    else:
        directory = params.get("directory")
        root, = store.roots(directory="input" if directory is None else directory)
    path = params.get("path")
    if not isinstance(path, str):
        raise ValueError("Expected a root-relative file path")
    return root, path


def verify_expected(info, params):
    if ((params.get("revision") is not None
         and tuple(params["revision"]) != astuple(info.revision))
            or (info.sha256 is not None and params.get("sha256") is not None
                and params["sha256"] != info.sha256)):
        raise FileChangedError("The selected native file changed")


async def file_roots(runtime, session_id, directory=None, category=None, **_):
    runtime._session(session_id)
    store = runtime._file_store()
    roots = await _io(store.roots, directory=directory, category=category)
    return [root_wire(store, root) for root in roots]


async def file_stat(runtime, session_id, checksum=False, descriptor=None, **params):
    runtime._session(session_id)
    if type(checksum) is not bool:
        raise ValueError("checksum must be bool")
    store = runtime._file_store()
    if descriptor is None:
        root, path = selection(store, params)
    else:
        saved = _descriptor(descriptor)
        if any(key in params for key in ("root_id", "directory", "path", "revision", "sha256")):
            raise ValueError("A file descriptor cannot be combined with another file selector")
        # The saved path is a constraint on current configuration, never a root
        # grant. In particular, do not fall back to another same-named file.
        roots = store.roots(directory=saved.directory, category=saved.category)
        matching = [candidate for candidate in roots if str(candidate.path) == saved.root_path]
        if len(matching) != 1:
            raise ValueError("The saved file root is not in the current native configuration")
        root, path = matching[0], saved.relative_path
        checksum = True
    download = await _io(store.open_download, root, path,
                         checksum=checksum,
                         _cancel_cleanup=lambda value: value.close())
    try:
        verify_expected(download.metadata, params)
        if descriptor is not None and (download.metadata.size != saved.size
                                       or download.metadata.sha256 != saved.sha256):
            raise FileChangedError("The saved file size or SHA256 does not match")
        return metadata_wire(store, download.metadata)
    finally:
        download.close()


async def file_remove(runtime, session_id, missing_ok=False, **params):
    runtime._session(session_id)
    if type(missing_ok) is not bool:
        raise ValueError("missing_ok must be bool")
    store = runtime._file_store()
    root, path = selection(store, params)
    revision = Revision(*params["revision"]) if params.get("revision") is not None else None
    await _io(store.remove, root, path, revision=revision, missing_ok=missing_ok)
    if callable(getattr(runtime, "file_changed", None)):
        await runtime.file_changed(root)
    return {"removed": True}


async def _small(read):
    data = bytearray()
    while chunk := await read(8192):
        data.extend(chunk)
        if len(data) > FILE_METADATA_LIMIT:
            raise ValueError("File metadata exceeds 64 KiB")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("File metadata must be an object")
    return value


@asynccontextmanager
async def _active(runtime, request, session):
    if session == "raw":
        raise SessionClosed("File I/O requires an SDK session")
    runtime._session(session)
    transport = request.transport
    if transport is None or transport.is_closing():
        raise ConnectionError("File connection closed before the commit point")
    runtime.requests.setdefault(session, set()).add(transport)
    try:
        yield
    finally:
        requests = runtime.requests.get(session)
        if requests is not None:
            requests.discard(transport)
            if not requests:
                runtime.requests.pop(session, None)


def install_file_routes(app, runtime):
    registry = TransferRegistry(runtime)
    app["file_transfers"] = registry
    registry.install(app)
    async def upload(request):
        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.name != "metadata":
            raise ValueError("Expected file metadata first")
        params = await _small(part.read_chunk)
        session = params.get("session_id")
        staged = None
        async with _active(runtime, request, session):
            store = runtime._file_store()
            root, path = selection(store, params)
            try:
                staged = await _io(store.begin_upload, root, path, size=params.get("size"),
                                   _cancel_cleanup=lambda value: value.abort())
                part = await reader.next()
                if part is None or part.name != "file":
                    raise ValueError("Expected one binary file part")
                while chunk := await part.read_chunk(FILE_CHUNK_SIZE):
                    runtime._session(session)
                    await _io(staged.write, chunk)
                part = await reader.next()
                if part is None or part.name != "verified":
                    raise ValueError("Expected the client verification footer")
                verified = await _small(part.read_chunk)
                if (type(verified.get("size")) is not int or verified["size"] != params["size"]
                        or await reader.next() is not None):
                    raise ValueError("Unexpected file size or multipart content")
                # MIME permits an epilogue after the closing boundary. Some of
                # it may already be inside MultipartReader's own buffer.
                trailing = 0
                while chunk := await request.content.read(8192):
                    trailing += len(chunk)
                    if trailing > FILE_METADATA_LIMIT:
                        raise ValueError("Multipart epilogue exceeds the metadata limit")
                runtime._session(session)
                if request.transport is None or request.transport.is_closing():
                    raise ConnectionError("File connection closed before the commit point")
                info = await _io(staged.commit, verified.get("sha256"))
                if callable(getattr(runtime, "file_changed", None)):
                    await runtime.file_changed(root)
                return web.json_response({"result": metadata_wire(store, info)})
            finally:
                if staged is not None:
                    await _io(staged.abort)

    async def download(request):
        params = await _small(request.content.read)
        session = params.get("session_id")
        response = None
        source = None
        async with _active(runtime, request, session):
            store = runtime._file_store()
            root, path = selection(store, params)
            try:
                source = await _io(store.open_download, root, path,
                                   checksum=False,
                                   _cancel_cleanup=lambda value: value.close())
                verify_expected(source.metadata, params)
                if params.get("sha256") is not None:
                    source.metadata = replace(source.metadata, sha256=params["sha256"])
                metadata = json.dumps(metadata_wire(store, source.metadata),
                                      ensure_ascii=True).encode()
                if len(metadata) > FILE_METADATA_LIMIT:
                    raise ValueError("File metadata exceeds 64 KiB")
                response = web.StreamResponse(headers={"Content-Type": FILE_CONTENT_TYPE})
                footer_size = len(json.dumps({"size": source.metadata.size, "sha256": "0" * 64}).encode())
                response.content_length = 4 + len(metadata) + source.metadata.size + 4 + footer_size
                await response.prepare(request)
                await response.write(len(metadata).to_bytes(4, "big") + metadata)
                chunks = source.chunks()
                while (chunk := await _io(next, chunks, None)) is not None:
                    runtime._session(session)
                    await response.write(chunk)
                runtime._session(session)
                verified = json.dumps({"size": source.metadata.size,
                                       "sha256": source.metadata.sha256}).encode()
                await response.write(len(verified).to_bytes(4, "big") + verified)
                await response.write_eof()
                return response
            except BaseException as error:
                if response is not None and response.prepared:
                    if request.transport is not None:
                        request.transport.close()
                    if isinstance(error, Exception):
                        return response
                raise
            finally:
                if source is not None:
                    source.close()

    app.router.add_post("/files/upload", upload)
    app.router.add_post("/files/download", download)


class TransferExpiredError(Exception):
    code = "FileTransferExpired"


class TransferBusyError(Exception):
    code = "FileTransferBusy"


class _TransferFailure(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass
class _Transfer:
    owner: str
    kind: str
    params: dict
    store: object
    root: object
    path: str
    admission_deadline: float
    metadata: dict | None = None
    upload: object = None
    result: dict | None = None
    failure: tuple[str, str] | None = None
    touched: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_task: asyncio.Task | None = None
    active_transport: object = None
    committing: bool = False


class TransferRegistry:
    """Process-local receipts and bounded partials; a new epoch never replays IDs.

    Only a connection failure leaves an upload alive. Explicit cancellation and
    application failures release its staging file. Receipts survive completion
    for the configured idle window so a lost commit reply can be queried safely.
    """

    def __init__(self, runtime):
        self.runtime = runtime
        self.epoch = uuid.uuid4().hex
        self.entries = {}
        self._admission = asyncio.Lock()
        self._window_secret = secrets.token_bytes(32)
        self._changed = asyncio.Event()

    @staticmethod
    def _owner(request):
        return hashlib.sha256(request.headers.get("Authorization", "loopback").encode()).hexdigest()

    def _identity(self, params):
        if params.get("epoch") != self.epoch:
            raise TransferExpiredError("File transfer daemon epoch changed; outcome is unknown")
        identity = params.get("transfer_id")
        if not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{32}", identity) is None:
            raise ValueError("Expected a UUID file transfer identity")
        return identity

    async def _find(self, request, params):
        await self.expire()
        identity = self._identity(params)
        entry = self.entries.get(identity)
        if entry is None or entry.owner != self._owner(request):
            raise TransferExpiredError("File transfer is unknown or expired; it will not be replayed")
        if entry.result is None and entry.store is not self.runtime._file_store():
            self._fail(entry, FileChangedError("Native file roots changed during the transfer"))
        entry.touched = time.monotonic()
        return entry

    def _wire(self, entry):
        if entry.failure is not None:
            raise _TransferFailure(*entry.failure)
        return {"epoch": self.epoch, "offset": entry.upload.written if entry.upload else 0,
                "metadata": entry.metadata, "result": entry.result}

    @staticmethod
    def _fail(entry, error):
        # A retained receipt must never retain an HTTP request/body traceback.
        entry.failure = (getattr(error, "code", type(error).__name__), str(error))

    async def expire(self):
        now = time.monotonic()
        for identity, entry in tuple(self.entries.items()):
            if not entry.lock.locked() and now - entry.touched >= entry.store.transfer_idle_timeout:
                async with entry.lock:
                    if entry.upload is not None:
                        try:
                            await _io(entry.upload.abort)
                        except OSError as error:
                            self._fail(entry, error)
                    if now >= entry.admission_deadline:
                        self.entries.pop(identity, None)
                    else:
                        # A policy can be shortened after admission. Keep only
                        # a bounded tombstone until its start window expires.
                        self._fail(entry, TransferExpiredError("File transfer idle window expired"))

    def _window_mac(self, owner, deadline):
        return hmac.new(self._window_secret, f"{self.epoch}:{owner}:{deadline}".encode(),
                        hashlib.sha256).hexdigest()

    def _window_deadline(self, owner, token):
        try:
            deadline, signature = token.split(":")
            valid = hmac.compare_digest(signature, self._window_mac(owner, deadline))
            value = float(deadline)
        except (ValueError, AttributeError, TypeError):
            valid, value = False, 0.0
        if not valid or value <= time.monotonic():
            raise TransferExpiredError("File admission window expired; an uncertain start will not be replayed")
        return value

    async def window(self, request):
        params = await _small(request.content.read)
        async with _active(self.runtime, request, params.get("session_id")):
            self._identity(params)
            store = self.runtime._file_store()
            deadline = f"{time.monotonic() + store.transfer_idle_timeout:.9f}"
            token = deadline + ":" + self._window_mac(self._owner(request), deadline)
            return web.json_response({"result": {"epoch": self.epoch, "window": token}})

    async def _new(self, request, params, kind):
        identity = self._identity(params)
        owner = self._owner(request)
        clean = {key: value for key, value in params.items()
                 if key not in {"session_id", "epoch", "transfer_id", "window"}}
        async with self._admission:
            await self.expire()
            existing = self.entries.get(identity)
            if existing is not None:
                if (existing.owner != owner or existing.kind != kind or existing.params != clean):
                    raise ValueError("File transfer identity was reused for different content")
                return existing
            deadline = self._window_deadline(owner, params.get("window"))
            store = self.runtime._file_store()
            pending = [value for value in self.entries.values()
                       if value.result is None and value.failure is None]
            if (len(pending) >= store.transfer_limit
                    or sum(value.owner == owner for value in pending) >= store.transfer_owner_limit
                    or len(self.entries) >= max(1024, store.transfer_limit * 8)):
                raise TransferBusyError("File transfer capacity is busy; retry the same identity")
            root, path = selection(store, clean)
            entry = _Transfer(owner, kind, clean, store, root, path, deadline)
            if kind == "upload":
                entry.upload = await _io(store.begin_upload, root, path, size=clean.get("size"),
                                         _cancel_cleanup=lambda value: value.abort())
            else:
                source = await _io(store.open_download, root, path, checksum=False,
                                   _cancel_cleanup=lambda value: value.close())
                try:
                    verify_expected(source.metadata, clean)
                    entry.metadata = metadata_wire(store, source.metadata)
                finally:
                    source.close()
            self.entries[identity] = entry
            self._changed.set()
            return entry

    @staticmethod
    def _params(request):
        return {"session_id": request.headers.get("X-Comani-Session"),
                "epoch": request.headers.get("X-Comani-Epoch"),
                "transfer_id": request.match_info["transfer_id"]}

    async def start(self, request):
        params = await _small(request.content.read)
        async with _active(self.runtime, request, params.get("session_id")):
            entry = await self._new(request, params, request.match_info["kind"])
            async with entry.lock:
                entry.touched = time.monotonic()
                return web.json_response({"result": self._wire(entry)})

    async def status(self, request):
        params = await _small(request.content.read)
        async with _active(self.runtime, request, params.get("session_id")):
            entry = await self._find(request, params)
            async with entry.lock:
                return web.json_response({"result": self._wire(entry)})

    async def append(self, request):
        params = self._params(request)
        async with _active(self.runtime, request, params["session_id"]):
            entry = await self._find(request, params)
            async with entry.lock:
                self._wire(entry)
                if entry.kind != "upload" or entry.result is not None:
                    raise ValueError("Transfer cannot accept upload bytes")
                if request.headers.get("X-Comani-Offset") != str(entry.upload.written):
                    raise ValueError("Upload offset does not match the confirmed offset")
                entry.active_task = asyncio.current_task()
                entry.active_transport = request.transport
                try:
                    while chunk := await asyncio.wait_for(
                            request.content.read(min(FILE_CHUNK_SIZE, entry.store.chunk_size)),
                            entry.store.transfer_idle_timeout):
                        self.runtime._session(params["session_id"])
                        await _io(entry.upload.write, chunk)
                        entry.touched = time.monotonic()
                except TimeoutError:
                    error = TransferExpiredError("File upload idle window expired")
                    self._fail(entry, error)
                    await _io(entry.upload.abort)
                    raise error from None
                except (ConnectionError, asyncio.CancelledError):
                    raise
                except BaseException as error:
                    # aiohttp marks an interrupted request body with a payload
                    # error rather than ConnectionError on some releases.
                    from aiohttp import ClientPayloadError
                    from aiohttp.web_protocol import RequestPayloadError

                    if isinstance(error, (ClientPayloadError, RequestPayloadError)):
                        raise
                    self._fail(entry, error)
                    await _io(entry.upload.abort)
                    raise
                finally:
                    entry.active_task = entry.active_transport = None
                    entry.touched = time.monotonic()
                return web.json_response({"result": self._wire(entry)})

    async def commit(self, request):
        params = await _small(request.content.read)
        async with _active(self.runtime, request, params.get("session_id")):
            entry = await self._find(request, params)
            async with entry.lock:
                self._wire(entry)
                if entry.kind != "upload":
                    raise ValueError("Expected an upload transfer")
                if entry.result is None:
                    def remember(info):
                        entry.result = metadata_wire(entry.store, info)

                    entry.committing = True
                    try:
                        info = await _io(entry.upload.commit, params.get("sha256"),
                                         _cancel_cleanup=remember)
                    except BaseException as error:
                        # Cancellation may arrive after the worker's atomic
                        # publication. Preserve its receipt before propagating.
                        if isinstance(error, asyncio.CancelledError):
                            if entry.result is not None and callable(getattr(self.runtime, "file_changed", None)):
                                pending = asyncio.create_task(self.runtime.file_changed(entry.root))
                                while not pending.done():
                                    try:
                                        await asyncio.shield(pending)
                                    except asyncio.CancelledError:
                                        continue
                                pending.result()
                            raise
                        self._fail(entry, error)
                        raise
                    else:
                        remember(info)
                        if callable(getattr(self.runtime, "file_changed", None)):
                            await self.runtime.file_changed(entry.root)
                    finally:
                        entry.committing = False
                elif params.get("sha256") != entry.result["sha256"]:
                    raise ValueError("Committed upload digest does not match")
                return web.json_response({"result": self._wire(entry)})

    async def cancel(self, request):
        params = await _small(request.content.read)
        async with _active(self.runtime, request, params.get("session_id")):
            entry = await self._find(request, params)
            # Mark cancellation before waiting for a long body/worker. Queued
            # append/commit requests must see the terminal state, not revive it.
            # A commit already admitted under its lock owns the publication
            # point: let it drain and preserve its receipt if it succeeds.
            if entry.result is None and not entry.committing:
                self._fail(entry, TransferExpiredError("File transfer was explicitly cancelled"))
            if entry.active_task is not None:
                entry.active_task.cancel()
                if entry.active_transport is not None:
                    entry.active_transport.abort()

            async def finish():
                async with entry.lock:
                    if entry.result is None:
                        if entry.upload is not None:
                            await _io(entry.upload.abort)
                        self._fail(entry, TransferExpiredError("File transfer was explicitly cancelled"))
                    return entry.result is None

            # Drain the affected disk worker and cleanup even if the control
            # request is itself cancelled repeatedly. No other entry is held.
            pending = asyncio.create_task(finish())
            cancelled = False
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    cancelled = True
            result = pending.result()
            if cancelled:
                raise asyncio.CancelledError
            return web.json_response({"result": {"cancelled": result}})

    async def data(self, request):
        params = self._params(request)
        response = source = None
        async with _active(self.runtime, request, params["session_id"]):
            entry = await self._find(request, params)
            async with entry.lock:
                self._wire(entry)
                if entry.kind != "download":
                    raise ValueError("Expected a download transfer")
                size = entry.metadata["size"]
                value = request.headers.get("Range")
                if value is None:
                    offset = 0
                elif re.fullmatch(r"bytes=[0-9]+-", value):
                    offset = int(value[6:-1])
                    if offset >= size:
                        raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"})
                else:
                    raise ValueError("Expected a single open-ended byte range")
                entry.active_task = asyncio.current_task()
                entry.active_transport = request.transport
                try:
                    source = await _io(entry.store.open_download, entry.root, entry.path,
                                       checksum=False, _cancel_cleanup=lambda value: value.close())
                    verify_expected(source.metadata, {"revision": entry.metadata["revision"]})
                    headers = {"Content-Type": "application/octet-stream",
                               "Accept-Ranges": "bytes", "X-Comani-Epoch": self.epoch,
                               "X-Comani-Offset": str(offset)}
                    if value is not None:
                        headers["Content-Range"] = f"bytes {offset}-{size - 1}/{size}"
                    response = web.StreamResponse(status=206 if value is not None else 200,
                                                  headers=headers)
                    response.content_length = size - offset
                    await response.prepare(request)
                    chunks = source.chunks(offset)
                    while (chunk := await _io(next, chunks, None)) is not None:
                        self.runtime._session(params["session_id"])
                        entry.touched = time.monotonic()
                        await asyncio.wait_for(response.write(chunk), entry.store.transfer_idle_timeout)
                    entry.result = metadata_wire(entry.store, source.metadata)
                    await response.write_eof()
                    return response
                except BaseException as error:
                    if not isinstance(error, (ConnectionError, asyncio.CancelledError)):
                        self._fail(entry, error)
                    if response is not None and response.prepared:
                        if request.transport is not None:
                            request.transport.close()
                        if isinstance(error, Exception):
                            return response
                    raise
                finally:
                    entry.active_task = entry.active_transport = None
                    entry.touched = time.monotonic()
                    if source is not None:
                        source.close()

    async def verify(self, request):
        """Only recovery can require this full scan after a lost final EOF."""
        params = await _small(request.content.read)
        async with _active(self.runtime, request, params.get("session_id")):
            entry = await self._find(request, params)
            async with entry.lock:
                self._wire(entry)
                if entry.kind != "download":
                    raise ValueError("Expected a download transfer")
                if entry.result is None:
                    source = await _io(entry.store.open_download, entry.root, entry.path,
                                       checksum=True, _cancel_cleanup=lambda value: value.close())
                    try:
                        verify_expected(source.metadata, {"revision": entry.metadata["revision"]})
                        entry.result = metadata_wire(entry.store, source.metadata)
                    finally:
                        source.close()
                return web.json_response({"result": self._wire(entry)})

    def install(self, app):
        app.router.add_post("/files/transfers/{kind:upload|download}/start", self.start)
        app.router.add_post("/files/transfers/window", self.window)
        app.router.add_post("/files/transfers/status", self.status)
        app.router.add_post("/files/transfers/commit", self.commit)
        app.router.add_post("/files/transfers/cancel", self.cancel)
        app.router.add_post("/files/transfers/verify", self.verify)
        app.router.add_post("/files/transfers/{transfer_id}/append", self.append)
        app.router.add_get("/files/transfers/{transfer_id}/data", self.data)

        async def lifetime(_):
            async def clean():
                while True:
                    interval = min((entry.store.transfer_idle_timeout / 2
                                    for entry in self.entries.values()), default=30.0)
                    try:
                        await asyncio.wait_for(self._changed.wait(), min(30.0, interval))
                    except TimeoutError:
                        pass
                    self._changed.clear()
                    await self.expire()

            task = asyncio.create_task(clean())
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                for entry in tuple(self.entries.values()):
                    async with entry.lock:
                        if entry.upload is not None:
                            await _io(entry.upload.abort)
                self.entries.clear()

        app.cleanup_ctx.append(lifetime)
