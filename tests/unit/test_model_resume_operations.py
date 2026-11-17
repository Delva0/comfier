"""Resume storage remains behind the real management and publication paths."""

import asyncio
import hashlib
from types import SimpleNamespace

import pytest
from aiohttp import web

from comani import Client, models
from comani.daemon.management.model_resume import ModelResumeCache
from comani.daemon.management.service import ManagementService

from .test_binary_fetch import serve
from .test_management_sdk import managed_server
from .test_management_service import Supervisor, finish, submit


@pytest.mark.parametrize("interruption", ["failure", "cancel"])
async def test_directory_snapshot_reuses_finished_artifact_and_stays_atomic(tmp_path, interruption):
    payloads = {"first": b"first" * 50000, "second": b"second" * 100000}
    calls, ranges = [], []
    started, release = asyncio.Event(), asyncio.Event()
    interrupted = True

    async def source(request):
        name = request.match_info["name"]
        calls.append(name)
        payload = payloads[name]
        start = int(request.headers.get("Range", "bytes=0-")[6:].split("-", 1)[0])
        ranges.append((name, start))
        if name == "second" and interrupted and interruption == "failure":
            raise web.HTTPServiceUnavailable()
        headers = {"Content-Length": str(len(payload) - start)}
        if start:
            headers["Content-Range"] = f"bytes {start}-{len(payload)-1}/{len(payload)}"
        response = web.StreamResponse(status=206 if start else 200, headers=headers)
        await response.prepare(request)
        try:
            if name == "second" and interrupted:
                await response.write(payload[start:start + 131072])
                started.set()
                await release.wait()
                await response.write(payload[start + 131072:])
            else:
                await response.write(payload[start:])
            await response.write_eof()
        except ConnectionError:
            pass
        return response

    app = web.Application()
    app.router.add_get("/{name}", source)
    service = ManagementService(Supervisor(tmp_path))
    root, = service.supervisor.file_store.roots(category="checkpoints")
    try:
        async with serve(app) as url:
            manifest = {"files": [{"provider": "url", "source": {"url": url + "/" + name},
                                   "filename": name, "size": len(payload),
                                   "sha256": hashlib.sha256(payload).hexdigest()}
                                  for name, payload in payloads.items()]}
            arguments = {"manifest": manifest, "root": root.id, "path": "bundle",
                         "publish": "directory", "transport": "http"}
            _, operation = await submit(service, "models", "snapshot", arguments, key="first")
            if interruption == "cancel":
                await asyncio.wait_for(started.wait(), 3)
                cache = tmp_path / ".comani" / "model-resume"
                for _ in range(100):
                    if any(path.stat().st_size == 131072 for path in cache.glob("*/payload.part")):
                        break
                    await asyncio.sleep(.01)
                assert not (root.path / "bundle").exists()
                assert await service.models.clear_partial() == {"removed": 0, "skipped_active": 2}
                await service.dispatch("management_cancel", operation, "user")
            first = await finish(service, operation)
            assert first["state"] == ("cancelled" if interruption == "cancel" else "failed")
            assert not (root.path / "bundle").exists()
            assert not list(tmp_path.glob(".comani-snapshot-*"))
            assert calls.count("first") == 1
            interrupted = False
            release.set()
            _, operation = await submit(service, "models", "snapshot", arguments, key="second")
            second = await finish(service, operation)
            assert second["state"] == "succeeded", second
            assert calls.count("first") == 1  # complete cached SHA is checked, with no provider traffic
            for name, payload in payloads.items():
                assert (root.path / "bundle" / name).read_bytes() == payload
            if interruption == "cancel":
                assert ("second", 131072) in ranges
            assert not list((tmp_path / ".comani" / "model-resume").glob("*/payload.part*"))
    finally:
        release.set()
        await service.close()


async def test_explicit_partial_cleanup_is_wired_through_http_and_both_sdk_scopes(tmp_path):
    destination = tmp_path / "models"
    destination.mkdir()
    value = destination.stat()
    identity = SimpleNamespace(root=SimpleNamespace(path=destination),
                               identity=(value.st_dev, value.st_ino, value.st_uid, value.st_gid, value.st_mode))
    complete = destination / "published.bin"
    complete.write_bytes(b"published model")
    cache = ModelResumeCache(tmp_path)
    entry = cache.acquire(identity, "unfinished.bin", "1" * 64, "http")
    entry.target.write_bytes(b"partial")
    async with managed_server(tmp_path) as (_, supervisor, url):
        async with Client(url) as remote:
            assert dict(await remote.models.clear_partial()) == {"removed": 0, "skipped_active": 1}
            entry.close()
            assert dict(await models.clear_partial()) == {"removed": 1, "skipped_active": 0}
            assert supervisor.process is None
    assert complete.read_bytes() == b"published model"
