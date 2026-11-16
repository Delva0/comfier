"""Real aria2 acquisition, origin isolation and mutable URL consistency."""

import asyncio
import hashlib
import shutil

import pytest
from aiohttp import web

from comani.daemon.management import transports
from comani.daemon.management.service import ManagementService
from comani.daemon.management.transports import select_transport

from .test_management_service import Supervisor, finish, submit


async def serve(handler):
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"


async def acquire(tmp_path, url, payload, *, digest=True, **options):
    service = ManagementService(Supervisor(tmp_path))
    root, = service.supervisor.file_store.roots(category="checkpoints")
    artifact = {"provider": "url", "source": {"url": url}, "filename": "weights.bin",
                "size": len(payload)}
    if digest:
        artifact["sha256"] = hashlib.sha256(payload).hexdigest()
    try:
        _, accepted = await submit(service, "models", "download",
                                   {"artifact": artifact, "root": root.id, **options})
        result = await asyncio.wait_for(finish(service, accepted), 15)
        return result, root.path / "weights.bin"
    finally:
        await service.close()


def require_aria2():
    if not shutil.which("aria2c"):
        pytest.skip("Real aria2 transfer requires the daemon's aria2c dependency")


def test_auto_selects_aria2_even_with_hub_available_or_no_digest():
    assert select_transport({"provider": "huggingface"}, "auto", hf_available=True) == "aria2"
    assert select_transport({"provider": "url"}, "auto") == "aria2"
    assert select_transport({}, "http") == "http"
    assert select_transport({}, "hf") == "hf"


async def test_missing_aria2_without_legal_installer_is_explicit_and_never_uses_http(tmp_path, monkeypatch):
    requests = []

    async def source(request):
        requests.append(request.method)
        return web.Response(body=b"model")

    runner, url = await serve(source)
    monkeypatch.setattr(transports.shutil, "which", lambda _: None)
    try:
        result, path = await acquire(tmp_path, url, b"model")
        assert result["state"] == "failed" and result["error"]["code"] == "DownloadToolInstallationFailed"
        assert result["error"]["details"]["phase"] == "select_installer"
        assert result["progress"]["transport"] == "aria2"
        assert result["progress"]["requested_transport"] == "auto"
        assert not requests and not path.exists()
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("route,expected_route", [
    ("redirect", "resolved_anonymous"), ("protected", "credential_relay"),
    ("late-redirect", "credential_relay"),
])
async def test_authenticated_aria2_scopes_credentials_and_preserves_range(tmp_path, monkeypatch, route, expected_route):
    require_aria2()
    payload = bytes(range(256)) * 32768
    received = []
    target_url = None

    async def source(request):
        authenticated = request.path != "/payload"
        assert request.headers.get("Authorization") == ("Bearer owned-only" if authenticated else None)
        received.append((request.path, request.method, request.headers.get("Range")))
        if request.path == "/redirect" or request.path == "/late-redirect" and request.method == "GET":
            raise web.HTTPFound(target_url + "/payload")
        start, end, status = 0, len(payload) - 1, 200
        if header := request.headers.get("Range"):
            left, right = header.removeprefix("bytes=").split("-", 1)
            start, end, status = int(left), min(int(right) if right else end, end), 206
        headers = {"Accept-Ranges": "bytes", "ETag": '"fixed"', "Content-Length": str(end - start + 1)}
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{len(payload)}"
        return web.Response(status=status, body=payload[start:end + 1], headers=headers)

    first_runner, first_url = await serve(source)
    second_runner, target_url = await serve(source)
    monkeypatch.setattr(transports, "provider_headers", lambda address:
                        {"Authorization": "Bearer owned-only"} if address.startswith(first_url + "/") else {})
    try:
        result, path = await acquire(tmp_path, first_url + "/" + route, payload, connections=4)
        assert result["state"] == "succeeded", result
        assert result["result"]["transport"] == "aria2"
        assert result["progress"]["source_route"] == expected_route
        assert "owned-only" not in str(result)
        assert path.read_bytes() == payload
        assert any(method == "GET" and header and not header.startswith("bytes=0-")
                   for _, method, header in received)
    finally:
        await first_runner.cleanup()
        await second_runner.cleanup()


async def mutable_source(request, *, payload, etag=None, interrupt=False, changed=None):
    start, end, status = 0, len(payload) - 1, 200
    if header := request.headers.get("Range"):
        left, right = header.removeprefix("bytes=").split("-", 1)
        start, end, status = int(left), min(int(right) if right else end, end), 206
    headers = {"Content-Length": str(end - start + 1), "Accept-Ranges": "bytes"}
    if etag:
        headers["ETag"] = etag
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{len(payload)}"
    response = web.StreamResponse(status=status, headers=headers)
    await response.prepare(request)
    if request.method == "HEAD":
        return response
    try:
        for offset in range(start, end + 1, 262144):
            if interrupt and offset >= start + 1048576 and (changed is None or changed.is_set()):
                request.transport.close()
                return response
            await response.write(payload[offset:min(offset + 262144, end + 1)])
            await asyncio.sleep(.002)
        await response.write_eof()
    except ConnectionError:
        pass
    return response


async def test_unhashed_strong_etag_pins_ranges_and_restarts_after_version_change(tmp_path):
    require_aria2()
    old, new = b"a" * 8388608, b"b" * 8388608
    changed = asyncio.Event()
    requests = []
    gets = 0

    async def source(request):
        nonlocal gets
        if request.method == "GET":
            gets += 1
            if gets == 2:
                changed.set()
        etag = '"v2"' if changed.is_set() else '"v1"'
        requests.append({"method": request.method, "range": request.headers.get("Range"),
                         "if_match": request.headers.get("If-Match"), "etag": etag})
        if request.method == "GET" and request.headers.get("If-Match") != etag:
            return web.Response(status=412)
        return await mutable_source(request, payload=new if changed.is_set() else old, etag=etag,
                                    interrupt=gets == 1, changed=changed)

    runner, url = await serve(source)
    try:
        result, path = await acquire(tmp_path, url, new, digest=False, connections=4)
        assert result["state"] == "succeeded", result
        assert result["result"]["transport"] == "aria2" and result["result"]["verified_against"] is None
        assert result["progress"]["integrity"] == "etag"
        assert result["progress"]["attempt"] >= 2
        assert path.read_bytes() == new
        assert any(item["method"] == "GET" and item["etag"] == '"v2"'
                   and item["if_match"] == '"v1"' for item in requests)
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("etag", [None, 'W/"weak"', "unquoted"])
async def test_unhashed_without_validator_uses_one_stream_and_restarts_at_zero(tmp_path, etag):
    require_aria2()
    old, new = b"x" * 8388608, b"y" * 8388608
    gets = []

    async def source(request):
        if request.method == "GET":
            gets.append(request.headers.get("Range"))
        return await mutable_source(request, payload=new if len(gets) >= 2 else old,
                                    etag=etag, interrupt=request.method == "GET" and len(gets) == 1)

    runner, url = await serve(source)
    try:
        result, path = await acquire(tmp_path, url, new, digest=False, connections=4)
        assert result["state"] == "succeeded", result
        assert result["progress"]["connections"] == 1
        assert result["progress"]["integrity"] == "single_response"
        assert result["progress"]["attempt"] == 2
        assert gets == [None, None]
        assert path.read_bytes() == new
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("headers", [{"Content-Type": "text/html"}, {"Content-Encoding": "gzip"}])
async def test_unhashed_source_probe_rejects_html_or_encoded_model_bytes(tmp_path, headers):
    require_aria2()
    methods = []

    async def source(request):
        methods.append(request.method)
        return web.Response(body=b"not model bytes", headers=headers)

    runner, url = await serve(source)
    try:
        result, path = await acquire(tmp_path, url, b"not model bytes", digest=False)
        assert result["state"] == "failed" and result["error"]["code"] == "ProviderError"
        assert methods == ["HEAD"]
        assert not path.exists()
    finally:
        await runner.cleanup()


async def test_get_only_url_remains_downloadable_when_head_is_forbidden(tmp_path):
    require_aria2()
    payload = b"valid GET representation" * 131072

    async def source(request):
        if request.method == "HEAD":
            return web.Response(status=403)
        return await mutable_source(request, payload=payload, etag='"fixed"')

    runner, url = await serve(source)
    try:
        old_http, _ = await acquire(tmp_path / "explicit", url, payload, digest=False, transport="http")
        assert old_http["state"] == "succeeded"
        result, path = await acquire(tmp_path / "default", url, payload, digest=False)
        assert result["state"] == "succeeded", result
        assert result["result"]["transport"] == "aria2"
        assert path.read_bytes() == payload
    finally:
        await runner.cleanup()
