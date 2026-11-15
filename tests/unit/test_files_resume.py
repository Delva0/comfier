"""Real HTTP file transfers: wire drops, receipts, ranges, revision and quotas."""

import asyncio
import hashlib
import time
import uuid
from contextlib import asynccontextmanager

import pytest
from aiohttp import ClientSession, web

from comani import Client, FileTransferExpired, RemoteError
from comani.daemon.http import create_app
from comani.files import _FileTransfer

from .test_binary_fetch import serve
from .test_files_api import FileRuntime


@asynccontextmanager
async def resumable(tmp_path, middleware=None, **limits):
    runtime = FileRuntime(tmp_path / "native")
    for key, value in limits.items():
        setattr(runtime._files, key, value)
    app = create_app(runtime, token="file-token")
    registry = app["file_transfers"]
    original = runtime.handshake

    async def handshake(**params):
        value = await original(**params)
        value["capabilities"]["files_resume"] = 1
        value["file_transfer_epoch"] = registry.epoch
        value["file_limits"]["transfer_idle_timeout"] = runtime._files.transfer_idle_timeout
        return value

    runtime.handshake = handshake
    if middleware is not None:
        app.middlewares.append(middleware)
    async with serve(app) as url:
        yield runtime, registry, url


def source_file(tmp_path, size=3 * 1024 * 1024 + 17):
    path = tmp_path / "source.bin"
    path.write_bytes(bytes(range(251)) * (size // 251) + b"x" * (size % 251))
    return path


async def test_normal_roundtrip_single_pass(tmp_path, monkeypatch):
    source = source_file(tmp_path)
    from pathlib import Path

    original, reads = Path.open, []

    class Reader:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, key):
            return getattr(self.stream, key)

        def read(self, size):
            data = self.stream.read(size)
            reads.append(len(data))
            return data

    def opened(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        return Reader(value) if path == source and args == ("rb",) else value

    async with resumable(tmp_path) as (runtime, _, url):
        async with Client(url, token="file-token") as client:
            monkeypatch.setattr(Path, "open", opened)
            uploaded = await client.files.upload(source)
            assert sum(reads) == source.stat().st_size
            assert max(reads) <= 1024 * 1024
            server_reads = []
            original_download = runtime._files.open_download

            def download(*args, **kwargs):
                assert kwargs["checksum"] is False
                result = original_download(*args, **kwargs)
                inner = result._stream

                class Tracked:
                    def __getattr__(self, key):
                        return getattr(inner, key)

                    def read(self, size):
                        chunk = inner.read(size)
                        server_reads.append(len(chunk))
                        return chunk

                result._stream = Tracked()
                return result

            monkeypatch.setattr(runtime._files, "open_download", download)
            copied = await client.files.download(uploaded, tmp_path / "copy.bin")
            assert copied.sha256 == uploaded.sha256
            assert copied.local_path.read_bytes() == source.read_bytes()
            assert sum(server_reads) == uploaded.size
            assert not runtime._files._uploads


@pytest.mark.parametrize("endpoint", ["upload/start", "commit"])
async def test_lost_acceptance_and_commit_reply_do_not_duplicate(tmp_path, endpoint):
    lost = []

    @web.middleware
    async def drop(request, handler):
        response = await handler(request)
        if request.path.endswith("/" + endpoint) and not lost:
            lost.append(True)
            request.transport.abort()
        return response

    source = source_file(tmp_path)
    async with resumable(tmp_path, drop) as (runtime, registry, url):
        async with Client(url, token="file-token") as client:
            uploaded = await client.files.upload(source, path="once.bin")
            assert uploaded.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
            assert len(registry.entries) == 1
            assert len(list((tmp_path / "native").rglob("once.bin"))) == 1
            assert not runtime._files._uploads


async def test_upload_resumes_confirmed_nonzero_offset_after_socket_drop(tmp_path):
    dropped, offsets = [], []

    @web.middleware
    async def drop(request, handler):
        if request.path.endswith("/append"):
            offsets.append(int(request.headers["X-Comani-Offset"]))
            original = request._payload
            received = [0]

            class Payload:
                def __getattr__(self, key):
                    return getattr(original, key)

                async def read(self, size):
                    if received[0] >= 512 * 1024 and not dropped:
                        dropped.append(True)
                        request.transport.abort()
                        raise ConnectionResetError("simulated lost upload connection")
                    chunk = await original.read(size)
                    received[0] += len(chunk)
                    return chunk

            request._payload = Payload()
        return await handler(request)

    source = source_file(tmp_path, 12 * 1024 * 1024 + 7)
    async with resumable(tmp_path, drop) as (runtime, _, url):
        async with Client(url, token="file-token") as client:
            uploaded = await client.files.upload(source)
            assert uploaded.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
            assert offsets[0] == 0 and offsets[1] >= 512 * 1024
            assert not runtime._files._uploads
            assert not list((tmp_path / "native").rglob(".comani-*.part"))


async def test_download_resumes_range_after_socket_drop(tmp_path, monkeypatch):
    source = source_file(tmp_path, 8 * 1024 * 1024 + 7)
    ranges, lost = [], []

    @web.middleware
    async def capture(request, handler):
        if request.path.endswith("/data"):
            ranges.append(request.headers.get("Range"))
        return await handler(request)

    async with resumable(tmp_path, capture) as (runtime, registry, url):
        async with Client(url, token="file-token") as client:
            uploaded = await client.files.upload(source)
            original = web.StreamResponse.write

            async def write(response, data):
                await original(response, data)
                if response.content_type == "application/octet-stream" and not lost:
                    lost.append(True)
                    response._req.transport.abort()
                    raise ConnectionResetError("simulated download disconnect")

            monkeypatch.setattr(web.StreamResponse, "write", write)
            copied = await client.files.download(uploaded, tmp_path / "copy.bin")
            assert copied.local_path.read_bytes() == source.read_bytes()
            assert ranges[0] is None and ranges[1].startswith("bytes=")
            assert int(ranges[1][6:-1]) > 0
            assert len(registry.entries) == 2


async def test_expired_or_changed_epoch_never_restarts_identity(tmp_path):
    async with resumable(tmp_path, transfer_idle_timeout=0.02) as (_, registry, url):
        async with Client(url, token="file-token") as client:
            async with ClientSession(headers={"Authorization": "Bearer file-token"}) as http:
                identity = {"session_id": client.session_id, "epoch": registry.epoch,
                            "transfer_id": uuid.uuid4().hex}
                async with http.post(url + "/files/transfers/window", json=identity) as response:
                    window = (await response.json())["result"]["window"]
                async with http.post(url + "/files/transfers/upload/start", json={
                    **identity, "window": window, "directory": "input", "path": "partial.bin", "size": 3}) as response:
                    assert response.status == 200
                await asyncio.sleep(0.04)
                await registry.expire()
                async with http.post(url + "/files/transfers/status", json=identity) as response:
                    assert (await response.json())["error"]["code"] == "FileTransferExpired"
                # A lost old start response must not create the same transfer
                # again after eviction, even when the daemon epoch is intact.
                async with http.post(url + "/files/transfers/upload/start", json={
                    **identity, "window": window, "directory": "input", "path": "partial.bin", "size": 3}) as response:
                    assert (await response.json())["error"]["code"] == "FileTransferExpired"
                transfer = _FileTransfer(client.transport, client.session_id,
                                         {"epoch": "old"}, 1)
                with pytest.raises(FileTransferExpired):
                    await transfer.request("upload/start", size=0, directory="input", path="new.bin")
                assert not registry.entries
                assert not list(tmp_path.rglob(".comani-*.part"))


@pytest.mark.parametrize("clients", [1, 2])
async def test_32_concurrent_uploads_and_two_clients_backpressure(tmp_path, clients):
    source = source_file(tmp_path, 128 * 1024)
    async with resumable(tmp_path, transfer_owner_limit=3) as (runtime, registry, url):
        async with Client(url, token="file-token") as first, Client(url, token="file-token") as second:
            owners = [first] * 32 if clients == 1 else [first, second] * 16
            results = await asyncio.gather(*(client.files.upload(source, path=f"{index}.bin")
                for index, client in enumerate(owners)))
            assert len(results) == len({value.name for value in results}) == 32
            assert len(registry.entries) == 32
            assert not runtime._files._uploads


async def test_recovery_window_exceeds_old_sixty_seconds_and_resets_on_progress(monkeypatch):
    class Transport:
        def _file_connection(self):
            return object(), "http://unused"

    now = [0.0]
    transfer = _FileTransfer(Transport(), "session", {"epoch": "epoch",
                             "limits": {"transfer_idle_timeout": 3600}}, None)
    monkeypatch.setattr(time, "monotonic", lambda: now[0])

    async def advance(delay):
        now[0] += delay

    monkeypatch.setattr(asyncio, "sleep", advance)
    import aiohttp

    error = aiohttp.ClientConnectionError("offline")
    for _ in range(100):
        await transfer.pause(error)
    assert now[0] > 60 and transfer.since == 0
    now[0] = 3599.9
    await transfer.pause(error)
    transfer.connected()
    await transfer.pause(error)
    assert transfer.since >= 3600
    now[0] += 3601
    with pytest.raises(aiohttp.ClientConnectionError):
        await transfer.pause(error)


async def test_changed_remote_source_during_reconnect_is_rejected(tmp_path, monkeypatch):
    source = source_file(tmp_path, 3 * 1024 * 1024)
    async with resumable(tmp_path) as (_, _, url):
        async with Client(url, token="file-token") as client:
            uploaded = await client.files.upload(source)
            from pathlib import Path

            native = Path(uploaded.root.path) / uploaded.name
            original, changed = web.StreamResponse.write, []

            async def write(response, data):
                await original(response, data)
                if response.content_type == "application/octet-stream" and not changed:
                    changed.append(True)
                    native.write_bytes(b"changed")
                    response._req.transport.abort()
                    raise ConnectionResetError("drop after source changed")

            monkeypatch.setattr(web.StreamResponse, "write", write)
            target = tmp_path / "copy.bin"
            with pytest.raises(RemoteError):
                await client.files.download(uploaded, target)
            assert not target.exists() and not list(tmp_path.glob(".comani-*.part"))


@pytest.mark.parametrize("malformed", ["status200", "range", "offset", "status416"])
async def test_resume_never_appends_an_invalid_http_range(tmp_path, monkeypatch, malformed):
    source = source_file(tmp_path)
    async with resumable(tmp_path) as (_, _, url):
        async with Client(url, token="file-token") as client:
            uploaded = await client.files.upload(source)
            original_write, original_prepare = web.StreamResponse.write, web.StreamResponse.prepare
            lost, resumes = [], []

            async def write(response, data):
                await original_write(response, data)
                if response.content_type == "application/octet-stream" and not lost:
                    lost.append(True)
                    response._req.transport.abort()
                    raise ConnectionResetError("force one reconnect")

            async def prepare(response, request):
                if not response.prepared and request.path.endswith("/data") and request.headers.get("Range"):
                    resumes.append(True)
                    if malformed == "status200":
                        response.set_status(200)
                    elif malformed == "status416":
                        response.set_status(416)
                    elif malformed == "range":
                        response.headers["Content-Range"] = "bytes 0-5/6"
                    else:
                        response.headers["X-Comani-Offset"] = "0"
                return await original_prepare(response, request)

            monkeypatch.setattr(web.StreamResponse, "write", write)
            monkeypatch.setattr(web.StreamResponse, "prepare", prepare)
            target = tmp_path / "bad-range.bin"
            import aiohttp

            with pytest.raises((RemoteError, aiohttp.ClientResponseError)):
                await client.files.download(uploaded, target)
            assert len(resumes) == 1
            assert not target.exists() and not list(tmp_path.glob(".comani-*.part"))


async def test_temporary_outage_recovers_and_explicit_shorter_window_expires(tmp_path):
    until = [0.0]
    drops = []

    @web.middleware
    async def partition(request, handler):
        if "/files/transfers/" in request.path and not request.path.endswith("/cancel"):
            if time.monotonic() < until[0]:
                drops.append(True)
                request.transport.abort()
                return web.Response()
        return await handler(request)

    source = source_file(tmp_path, 256 * 1024)
    async with resumable(tmp_path, partition, transfer_idle_timeout=0.5) as (runtime, registry, url):
        async with Client(url, token="file-token") as client:
            until[0] = time.monotonic() + 0.15
            pending = asyncio.create_task(client.files.upload(source, path="recover.bin"))
            beats = 0
            while not pending.done():
                beats += 1
                await asyncio.sleep(0.01)
            uploaded = await pending
            assert uploaded.size == source.stat().st_size and beats >= 10 and drops
            until[0] = time.monotonic() + 0.5
            import aiohttp

            with pytest.raises((aiohttp.ClientConnectionError, TimeoutError)):
                await client.files.upload(source, path="never.bin", recovery_timeout=0.05)
            assert not runtime._files._uploads
            assert not any(entry.path == "never.bin" for entry in registry.entries.values())


async def test_upload_local_revision_change_during_drop_cleans_partial(tmp_path):
    source = source_file(tmp_path, 3 * 1024 * 1024)
    dropped = []

    @web.middleware
    async def drop(request, handler):
        if request.path.endswith("/append") and not dropped:
            original = request._payload
            received = []

            class Payload:
                def __getattr__(self, key):
                    return getattr(original, key)

                async def read(self, size):
                    if received:
                        dropped.append(True)
                        source.write_bytes(b"changed source")
                        request.transport.abort()
                        raise ConnectionResetError("source changed during disconnect")
                    chunk = await original.read(size)
                    received.append(True)
                    return chunk

            request._payload = Payload()
        return await handler(request)

    async with resumable(tmp_path, drop) as (runtime, _, url):
        async with Client(url, token="file-token") as client:
            with pytest.raises(OSError, match="source changed"):
                await client.files.upload(source, path="changed.bin")
            assert not runtime._files._uploads
            assert not list((tmp_path / "native").rglob("changed.bin"))
            assert not list((tmp_path / "native").rglob(".comani-*.part"))


async def test_partial_tampering_is_rejected_instead_of_publishing_stale_digest(tmp_path):
    source = source_file(tmp_path)
    async with resumable(tmp_path) as (runtime, _, url):
        async with Client(url, token="file-token") as client:
            from comani.daemon.files import FileChangedError

            root, = runtime._files.roots(directory="input")
            upload = runtime._files.begin_upload(root, "tampered.bin", size=4)
            upload.write(b"good")
            upload._partial.write_bytes(b"evil")
            # Some filesystems expose one timestamp per kernel tick. Make the
            # external revision change explicit instead of assuming sleep(0).
            import os

            os.utime(upload._partial, ns=(upload._revision.mtime_ns + 1_000_000,
                                         upload._revision.mtime_ns + 1_000_000))
            with pytest.raises(FileChangedError):
                upload.commit(hashlib.sha256(b"good").hexdigest())
            assert not list(root.path.rglob(".comani-*.part"))
            assert not (root.path / "tampered.bin").exists()
            result = await client.files.upload(source)
            assert result.size == source.stat().st_size


async def test_download_lost_last_eof_uses_same_receipt_and_recovery_only_scan(tmp_path, monkeypatch):
    source = source_file(tmp_path, 256 * 1024)
    async with resumable(tmp_path) as (runtime, registry, url):
        async with Client(url, token="file-token") as client:
            uploaded = await client.files.upload(source)
            original_write, lost = web.StreamResponse.write, []
            original_download, scans = runtime._files.open_download, []

            async def write(response, data):
                await original_write(response, data)
                if response.content_type == "application/octet-stream" and not lost:
                    lost.append(True)
                    response._req.transport.abort()
                    raise ConnectionResetError("last bytes arrived but the final verification was lost")

            def opened(*args, **kwargs):
                scans.append(kwargs["checksum"])
                return original_download(*args, **kwargs)

            monkeypatch.setattr(web.StreamResponse, "write", write)
            monkeypatch.setattr(runtime._files, "open_download", opened)
            copied = await client.files.download(uploaded, tmp_path / "last.bin")
            assert copied.local_path.read_bytes() == source.read_bytes()
            assert scans == [False, False, True]
            assert len(registry.entries) == 2


async def test_old_daemon_without_resume_capability_uses_legacy_single_attempt(tmp_path):
    runtime = FileRuntime(tmp_path / "native")
    app = create_app(runtime, token="file-token")
    # Remove only the negotiated capability, as a pre-resume daemon would.
    @web.middleware
    async def old_handshake(request, handler):
        response = await handler(request)
        if request.path == "/rpc" and isinstance(response, web.Response):
            import json

            payload = json.loads(response.body)
            result = payload.get("result", {})
            if isinstance(result, dict) and "capabilities" in result:
                result["capabilities"].pop("files_resume", None)
                result.pop("file_transfer_epoch", None)
                response.body = json.dumps(payload).encode()
        return response

    app.middlewares.append(old_handshake)
    source = source_file(tmp_path, 1024)
    async with serve(app) as url:
        async with Client(url, token="file-token") as client:
            assert "files_resume" not in client.handshake["capabilities"]
            uploaded = await client.files.upload(source)
            copied = await client.files.download(uploaded, tmp_path / "legacy.bin")
            assert copied.local_path.read_bytes() == source.read_bytes()
            assert not app["file_transfers"].entries


async def test_stalled_connected_upload_has_idle_cleanup(tmp_path):
    async with resumable(tmp_path, transfer_idle_timeout=0.08) as (runtime, registry, url):
        async with Client(url, token="file-token") as client:
            transfer = _FileTransfer(client.transport, client.session_id,
                                     {"epoch": registry.epoch}, 1)
            await transfer.request("upload/start", size=3, directory="input", path="stalled.bin")

            async def body():
                yield b"x"
                await asyncio.Event().wait()

            async with ClientSession(headers={"Authorization": "Bearer file-token"}) as http:
                async with http.post(url + "/files/transfers/" + transfer.identity["transfer_id"] + "/append",
                                     headers={**transfer.headers(), "X-Comani-Offset": "0"},
                                     data=body()) as response:
                    assert (await response.json())["error"]["code"] == "FileTransferExpired"
            assert not runtime._files._uploads
            assert not list(tmp_path.rglob(".comani-*.part"))


async def test_shortened_policy_retains_bounded_expired_start_tombstone(tmp_path):
    async with resumable(tmp_path, transfer_idle_timeout=1.0) as (runtime, registry, url):
        async with Client(url, token="file-token") as client:
            transfer = _FileTransfer(client.transport, client.session_id,
                                     {"epoch": registry.epoch}, 1)
            window = await transfer.request("window")
            await transfer.request("upload/start", window=window["window"], size=1,
                                   directory="input", path="old.bin")
            runtime._files.transfer_idle_timeout = 0.01
            await asyncio.sleep(0.03)
            await registry.expire()
            assert len(registry.entries) == 1
            assert not runtime._files._uploads
            with pytest.raises(FileTransferExpired):
                await transfer.request("upload/start", window=window["window"], size=1,
                                       directory="input", path="old.bin")
            assert not list(tmp_path.rglob(".comani-*.part"))


async def _half_open_append(transfer, received):
    async def body():
        yield b"abc"
        await asyncio.Event().wait()

    url = transfer.base + "/files/transfers/" + transfer.identity["transfer_id"] + "/append"
    try:
        async with transfer.http.post(url, data=body(), headers={**transfer.headers(),
                                      "X-Comani-Offset": "0"}) as response:
            return await response.json()
    finally:
        received.set()


async def test_cancel_interrupts_half_open_append_and_queued_commit_cannot_revive(tmp_path):
    async with resumable(tmp_path, transfer_idle_timeout=30) as (_, registry, url):
        async with Client(url, token="file-token") as client:
            transfer = _FileTransfer(client.transport, client.session_id, {"epoch": registry.epoch}, 2)
            await transfer.request("upload/start", size=1000, directory="input", path="cancelled.bin")
            finished = asyncio.Event()
            writer = asyncio.create_task(_half_open_append(transfer, finished))
            entry = registry.entries[transfer.identity["transfer_id"]]
            while entry.upload.written != 3:
                await asyncio.sleep(0.001)
            commit = asyncio.create_task(transfer.request("commit", sha256=hashlib.sha256(b"abc").hexdigest()))
            await asyncio.sleep(0)
            result = await asyncio.wait_for(transfer.request("cancel"), 0.5)
            assert result == {"cancelled": True}
            with pytest.raises(FileTransferExpired):
                await commit
            with pytest.raises(FileTransferExpired):
                await transfer.request("commit", sha256=hashlib.sha256(b"abc").hexdigest())
            assert await transfer.request("cancel") == {"cancelled": True}
            await asyncio.gather(writer, return_exceptions=True)
            assert finished.is_set()
            assert not list(tmp_path.rglob(".comani-*.part"))
            assert not list(tmp_path.rglob("cancelled.bin"))
            assert entry.active_task is None and entry.active_transport is None


async def test_cancel_drains_blocked_write_without_blocking_other_transfers(tmp_path, monkeypatch):
    import threading

    from comani.daemon.files import Upload

    entered, release = threading.Event(), threading.Event()
    original = Upload.write

    def blocked(upload, chunk):
        if upload.relative_path == "blocked.bin":
            entered.set()
            assert release.wait(3)
        return original(upload, chunk)

    monkeypatch.setattr(Upload, "write", blocked)
    async with resumable(tmp_path) as (_, registry, url):
        async with Client(url, token="file-token") as client:
            transfer = _FileTransfer(client.transport, client.session_id, {"epoch": registry.epoch}, 2)
            await transfer.request("upload/start", size=1000, directory="input", path="blocked.bin")
            writer = asyncio.create_task(_half_open_append(transfer, asyncio.Event()))
            assert await asyncio.to_thread(entered.wait, 1)
            first = asyncio.create_task(transfer.request("cancel"))
            second = asyncio.create_task(transfer.request("cancel"))
            try:
                await asyncio.sleep(0.02)
                assert not first.done() and not second.done()
                source = source_file(tmp_path, 1024)
                other = await asyncio.wait_for(client.files.upload(source, path="other.bin"), 0.5)
                assert other.size == 1024
            finally:
                release.set()
            assert await first == await second == {"cancelled": True}
            await asyncio.gather(writer, return_exceptions=True)
            assert not list(tmp_path.rglob(".comani-*.part"))
            assert not list(tmp_path.rglob("blocked.bin"))


async def test_commit_admitted_before_cancel_retains_publication_and_receipt(tmp_path, monkeypatch):
    import threading

    from comani.daemon.files import Upload

    entered, release = threading.Event(), threading.Event()
    original = Upload.commit

    def blocked(upload, digest):
        if upload.relative_path == "committed.bin":
            entered.set()
            assert release.wait(3)
        return original(upload, digest)

    monkeypatch.setattr(Upload, "commit", blocked)
    async with resumable(tmp_path) as (_, registry, url):
        async with Client(url, token="file-token") as client:
            transfer = _FileTransfer(client.transport, client.session_id, {"epoch": registry.epoch}, 2)
            await transfer.request("upload/start", size=3, directory="input", path="committed.bin")
            address = transfer.base + "/files/transfers/" + transfer.identity["transfer_id"] + "/append"
            async with transfer.http.post(address, data=b"abc", headers={**transfer.headers(),
                                          "X-Comani-Offset": "0"}) as response:
                assert response.status == 200
            digest = hashlib.sha256(b"abc").hexdigest()
            commit = asyncio.create_task(transfer.request("commit", sha256=digest))
            assert await asyncio.to_thread(entered.wait, 1)
            cancellation = asyncio.create_task(transfer.request("cancel"))
            try:
                await asyncio.sleep(0.02)
                assert not cancellation.done()
            finally:
                release.set()
            published = await commit
            assert await cancellation == {"cancelled": False}
            assert (await transfer.request("status"))["result"] == published["result"]
            assert (await transfer.request("commit", sha256=digest))["result"] == published["result"]
            assert list(tmp_path.rglob("committed.bin"))[0].read_bytes() == b"abc"
            assert not list(tmp_path.rglob(".comani-*.part"))
