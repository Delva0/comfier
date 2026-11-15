"""The bounded file wire protocol; no background upload jobs or ownership GC."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import astuple, replace

from aiohttp import web

from comani.files import FILE_CHUNK_SIZE, FILE_CONTENT_TYPE, FILE_METADATA_LIMIT, _io

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


async def file_stat(runtime, session_id, checksum=False, **params):
    runtime._session(session_id)
    if type(checksum) is not bool:
        raise ValueError("checksum must be bool")
    store = runtime._file_store()
    root, path = selection(store, params)
    download = await _io(store.open_download, root, path,
                         checksum=checksum,
                         _cancel_cleanup=lambda value: value.close())
    try:
        verify_expected(download.metadata, params)
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
