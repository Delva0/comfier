"""SDK file I/O through real authenticated HTTP, isolated from native/GPU files."""

import asyncio
import gc
import hashlib
import json
import os
import threading
import weakref
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from aiohttp import ClientPayloadError, ClientSession, FormData

from comani import Client, DownloadedFile, RemoteError, RemoteFile, SessionClosed
from comani._runtime.files import FileStore
from comani._runtime.http import create_app
from comani._runtime.service import NativeRuntime
from comani.files import _io

from .test_binary_fetch import serve
from .test_runtime_files import Folders


class FileRuntime(NativeRuntime):
    def __init__(self, base, **kwargs):
        super().__init__("/unused", **kwargs)
        self.adapter.node_types = lambda: {}
        self._files = FileStore(Folders(base), max_file_size=self.max_file_bytes)

    async def start(self):
        return self


@asynccontextmanager
async def running(tmp_path, **kwargs):
    runtime = FileRuntime(tmp_path / "native", **kwargs)
    async with serve(create_app(runtime, token="file-token")) as url:
        yield runtime, url


async def test_roundtrip_local_metadata_native_names_and_shared_lifetime(tmp_path):
    source = tmp_path / "local.png"
    data = bytes(range(256)) * 10000
    source.write_bytes(data)
    async with running(tmp_path) as (runtime, url):
        async with Client(url, token="file-token") as remote:
            assert remote.handshake["file_limits"]["max_file_bytes"] is None
            roots = await remote.files.roots()
            assert {root.directory for root in roots} == {"input", "temp", "output"}
            uploaded = await remote.files.upload(source, path="nested/pixel[output]")
            assert isinstance(uploaded, RemoteFile)
            assert uploaded.name == "nested/pixel[output]"
            assert uploaded.native_name == "nested/pixel[output] [input]"
            assert uploaded.sha256 == hashlib.sha256(data).hexdigest()
            assert (await remote.files.stat(uploaded)).sha256 is None
            assert uploaded == await remote.files.stat(uploaded, checksum=True)
            downloaded = await remote.files.download(uploaded, tmp_path / "copied.png")
            assert isinstance(downloaded, DownloadedFile)
            assert downloaded.local_path.read_bytes() == data
            assert downloaded.remote == uploaded
        assert (Path(uploaded.root.path) / uploaded.relative_path).read_bytes() == data
        async with Client(url, token="file-token") as another:
            assert await another.files.stat(uploaded, checksum=True) == uploaded
            await another.files.remove(uploaded)
            with pytest.raises(RemoteError):
                await another.files.stat(uploaded)
            await another.files.remove(uploaded, missing_ok=True)
        assert not runtime._files._uploads


async def test_model_roots_explicit_shadow_prevention_and_output_annotation(tmp_path):
    source = tmp_path / "model.safetensors"
    source.write_bytes(b"model")
    async with running(tmp_path) as (_, url):
        async with Client(url, token="file-token") as remote:
            first, second = await remote.files.roots(category="checkpoints")
            assert first.priority == 0 and second.priority == 1
            model = await remote.files.upload(source, root=second, path="model.safetensors")
            assert model.root == second and model.native_name == "model.safetensors"
            with pytest.raises(RemoteError):
                await remote.files.upload(source, root=first, path=model.name)
            output = await remote.files.upload(source, directory="output", path="saved.bin")
            assert output.native_name == "saved.bin [output]"
            copied = await remote.files.download("saved.bin", tmp_path / "out.bin", directory="output")
            assert copied.local_path.read_bytes() == b"model"


async def test_upload_create_only_and_download_default_no_overwrite(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    target = tmp_path / "existing.bin"
    target.write_bytes(b"keep")
    async with running(tmp_path) as (_, url):
        async with Client(url, token="file-token") as remote:
            info = await remote.files.upload(source, path="file.bin")
            with pytest.raises(RemoteError):
                await remote.files.upload(source, path="file.bin")
            with pytest.raises(FileExistsError):
                await remote.files.download(info, target)
            assert target.read_bytes() == b"keep"
            await remote.files.download(info, target, overwrite=True)
            assert target.read_bytes() == b"source"


async def test_proxy_owns_client_metadata_does_not_and_explicit_close_wins(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    async with running(tmp_path) as (_, url):
        remote = Client(url, token="file-token")
        owner = weakref.ref(remote)
        files = remote.files
        del remote
        gc.collect()
        assert owner() is not None
        metadata = await files.upload(source)
        await owner().close()
        with pytest.raises(SessionClosed):
            await files.stat(metadata)
        del files
        gc.collect()
        assert owner() is None
        assert metadata.size == 6


async def test_file_io_in_graph_scope_is_explicit_external_io(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    async with running(tmp_path) as (_, url):
        async with Client(url, token="file-token") as remote:
            with remote.graph() as graph:
                info = await remote.files.upload(source)
                assert info.size == 6
            assert graph.nodes == {}


async def test_auth_invalid_session_and_partial_verification_failure(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    async with running(tmp_path) as (runtime, url):
        with pytest.raises(RemoteError):
            async with Client(url, token="wrong") as wrong:
                await wrong.files.upload(source)
        async with Client(url, token="file-token") as remote:
            async with ClientSession(headers={"Authorization": "Bearer file-token"}) as http:
                payload = FormData()
                payload.add_field("metadata", '{"session_id":"bad","directory":"input","path":"bad.bin","size":3}',
                                  content_type="application/json")
                payload.add_field("file", b"bad", content_type="application/octet-stream")
                payload.add_field("verified", '{"size":3,"sha256":"wrong"}', content_type="application/json")
                async with http.post(url + "/files/upload", data=payload) as response:
                    assert (await response.json())["error"]["code"] == "SessionClosed"
                payload = FormData()
                payload.add_field("metadata", json.dumps({"session_id": remote.session_id,
                                  "directory": "input", "path": "bad.bin", "size": 3}),
                                  content_type="application/json")
                payload.add_field("file", b"bad", content_type="application/octet-stream")
                payload.add_field("verified", '{"size":3,"sha256":"wrong"}', content_type="application/json")
                async with http.post(url + "/files/upload", data=payload) as response:
                    assert response.status == 400
        assert not list(tmp_path.rglob(".comani-*.part"))
        assert not runtime._files._uploads


async def test_quota_is_optional_and_explicit_quota_is_enforced(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    async with running(tmp_path, max_file_bytes=4) as (runtime, url):
        async with Client(url, token="file-token") as remote:
            assert remote.handshake["file_limits"]["max_file_bytes"] == 4
            with pytest.raises(RemoteError):
                await remote.files.upload(source)
        assert not runtime._files._uploads


async def test_close_cancels_active_file_io_and_rejects_new_io(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    gate = asyncio.Event()

    async def paused(*_):
        gate.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("comani.files._upload_http", paused)
    async with running(tmp_path) as (_, url):
        remote = await Client(url, token="file-token").start()
        pending = asyncio.create_task(remote.files.upload(source))
        await gate.wait()
        await remote.close()
        with pytest.raises(SessionClosed):
            await pending
        assert not remote._file_tasks
        with pytest.raises(SessionClosed):
            await remote.files.roots()


async def test_download_is_single_pass_and_sends_metadata_before_reading_content(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    data = b"x" * (2 * 1024 * 1024 + 7)
    source.write_bytes(data)
    async with running(tmp_path) as (runtime, url):
        async with Client(url, token="file-token") as remote:
            uploaded = await remote.files.upload(source)
            original = runtime._files.open_download
            release = threading.Event()
            reads = []

            class PausedReader:
                def __init__(self, stream):
                    self.stream = stream

                def __getattr__(self, name):
                    return getattr(self.stream, name)

                def read(self, size):
                    if not release.wait(5):
                        raise TimeoutError("Test did not release the content reader")
                    chunk = self.stream.read(size)
                    reads.append(len(chunk))
                    return chunk

            def opened(*args, **kwargs):
                assert kwargs.get("checksum") is False
                download = original(*args, **kwargs)
                download._stream = PausedReader(download._stream)
                return download

            monkeypatch.setattr(runtime._files, "open_download", opened)
            async with ClientSession(headers={"Authorization": "Bearer file-token"}) as http:
                try:
                    async with asyncio.timeout(2):
                        response = await http.post(url + "/files/download", json={
                            "session_id": remote.session_id, "root_id": uploaded.root.id,
                            "path": uploaded.relative_path})
                        length = int.from_bytes(await response.content.readexactly(4), "big")
                        metadata = json.loads(await response.content.readexactly(length))
                    assert metadata["sha256"] is None and reads == []
                finally:
                    release.set()
                async with response:
                    assert await response.content.readexactly(len(data)) == data
                    length = int.from_bytes(await response.content.readexactly(4), "big")
                    verified = json.loads(await response.content.readexactly(length))
                    assert verified["sha256"] == hashlib.sha256(data).hexdigest()
                    assert await response.read() == b""
            assert sum(reads) == len(data) and max(reads) <= 1024 * 1024


async def test_stat_default_does_not_read_file_content(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    async with running(tmp_path) as (runtime, url):
        async with Client(url, token="file-token") as remote:
            uploaded = await remote.files.upload(source)
            original = runtime._files.open_download
            checksums = []

            def opened(*args, **kwargs):
                checksums.append(kwargs["checksum"])
                return original(*args, **kwargs)

            monkeypatch.setattr(runtime._files, "open_download", opened)
            assert (await remote.files.stat(uploaded)).sha256 is None
            assert (await remote.files.stat(uploaded, checksum=True)).sha256 == uploaded.sha256
            assert checksums == [False, True]


@pytest.mark.parametrize("failure", ["digest", "truncated"])
async def test_failed_download_verification_never_publishes_local_file(tmp_path, monkeypatch, failure):
    source = tmp_path / "source.bin"
    source.write_bytes(b"source" * 300000)
    destination = tmp_path / "downloaded.bin"
    async with running(tmp_path) as (runtime, url):
        async with Client(url, token="file-token") as remote:
            uploaded = await remote.files.upload(source)
            original = runtime._files.open_download

            def opened(*args, **kwargs):
                download = original(*args, **kwargs)
                chunks = download.chunks

                def broken():
                    for chunk in chunks():
                        yield chunk
                        if failure == "truncated":
                            raise OSError("Simulated read failure")
                    download.metadata = replace(download.metadata, sha256="0" * 64)

                download.chunks = broken
                return download

            monkeypatch.setattr(runtime._files, "open_download", opened)
            with pytest.raises((RemoteError, ClientPayloadError, asyncio.IncompleteReadError)):
                await remote.files.download(uploaded, destination)
            assert not destination.exists() and not list(destination.parent.glob(".comani-*.part"))


async def test_upload_source_change_omits_footer_and_cleans_remote_partial(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    original = Path.stat

    def changed(path, *args, **kwargs):
        if path == source:
            source.write_bytes(b"changed local source")
        return original(path, *args, **kwargs)

    async with running(tmp_path) as (runtime, url):
        async with Client(url, token="file-token") as remote:
            monkeypatch.setattr(Path, "stat", changed)
            with pytest.raises(OSError, match="source changed"):
                await remote.files.upload(source, path="changed.bin")
            for _ in range(100):
                if not runtime._files._uploads:
                    break
                await asyncio.sleep(0.01)
            assert not runtime._files._uploads
            assert not list((tmp_path / "native").rglob("changed.bin"))
            assert not list((tmp_path / "native").rglob(".comani-*.part"))


async def test_upload_stream_exceeds_old_json_body_limit_with_bounded_buffers(tmp_path):
    source = tmp_path / "large.bin"
    chunk = b"x" * (1024 * 1024)
    digest = hashlib.sha256()
    with source.open("wb") as stream:
        for _ in range(129):
            stream.write(chunk)
            digest.update(chunk)
    async with running(tmp_path) as (_, url):
        async with Client(url, token="file-token") as remote:
            uploaded = await remote.files.upload(source)
            assert uploaded.size == 129 * 1024 * 1024
            assert uploaded.sha256 == digest.hexdigest()
            assert os.stat(Path(uploaded.root.path) / uploaded.relative_path).st_size == uploaded.size


async def test_repeated_close_cancellation_drains_disk_work_and_closes_its_result():
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    cleaned = []

    class Transport:
        async def open(self):
            pass

        async def request(self, method, **_):
            return {"epoch": "test", "capabilities": {"files": 1}} if method == "handshake" else {}

        async def close(self):
            pass

    def blocking_create():
        started.set()
        if not release.wait(3):
            raise TimeoutError("Test did not release the disk worker")
        finished.set()
        return object()

    async def operation(*_):
        return await _io(blocking_create, _cancel_cleanup=lambda _: cleaned.append("closed"))

    remote = await Client(transport=Transport()).start()
    pending = asyncio.create_task(remote.files._run(operation))
    assert await asyncio.to_thread(started.wait, 2)
    closing = asyncio.create_task(remote.close())
    try:
        await asyncio.sleep(0.03)
        assert not closing.done() and not finished.is_set()
        pending.cancel()  # A caller can cancel again while Client.close is draining.
        await asyncio.sleep(0)
        assert not closing.done()
    finally:
        release.set()
    await asyncio.wait_for(closing, 2)
    with pytest.raises(SessionClosed):
        await pending
    assert finished.is_set() and cleaned == ["closed"]


async def test_http_upload_cancellation_drains_its_source_producer_before_closing(tmp_path, monkeypatch):
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    closes = []
    original = Path.open

    class BlockedReader:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def read(self, size):
            started.set()
            if not release.wait(3):
                raise TimeoutError("Test did not release the source reader")
            result = self.stream.read(size)
            finished.set()
            return result

        def close(self):
            closes.append(finished.is_set())
            self.stream.close()

    def opened(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        return BlockedReader(stream) if path == source else stream

    async with running(tmp_path) as (runtime, url):
        async with Client(url, token="file-token") as remote:
            monkeypatch.setattr(Path, "open", opened)
            pending = asyncio.create_task(remote.files.upload(source, path="cancelled.bin"))
            assert await asyncio.to_thread(started.wait, 2)
            pending.cancel()
            try:
                await asyncio.sleep(0.03)
                assert not pending.done() and closes == []
                pending.cancel()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert closes == [True]
            for _ in range(100):
                if not runtime._files._uploads:
                    break
                await asyncio.sleep(0.01)
            assert not runtime._files._uploads
            assert not list((tmp_path / "native").rglob("cancelled.bin"))
            assert not list((tmp_path / "native").rglob(".comani-*.part"))
