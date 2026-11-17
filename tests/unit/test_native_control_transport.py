"""Extension JSON requests retain the shared authenticated HTTP connection."""

import asyncio
import json

import pytest
from aiohttp import ClientResponseError, web

from comani import HTTPTransport, ZMQTransport
from comani.errors import RemoteError
from comani.transport import _native_path


@pytest.mark.parametrize("path", [
    "https://example.test/x", "//example.test/x", "relative", "/x?q=y", "/x#part", "/x\\y",
    "/manage", "/manage/child", "/rpc", "/files/download", "/prompt", "/history/id", "/queue", "/ws",
    "/%6danage", "/%256danage", "/extension/../rpc", "/extension/%2e%2e/rpc", "/%2fexample.test",
    "/x%3fy", "/x%23y", "/x%00y",
])
def test_native_control_rejects_other_origins_and_sdk_route_aliases(path):
    with pytest.raises(ValueError):
        _native_path(path)


@pytest.mark.parametrize("kind", ["http", "zmq"])
async def test_native_control_uses_http_once_and_zmq_delegates_to_the_companion(kind):
    if kind == "zmq":
        pytest.importorskip("zmq")
    calls = []

    async def extension(request):
        calls.append(request.method)
        assert request.headers["Authorization"] == "Bearer control-token"
        if request.method == "POST":
            return web.json_response({"body": await request.json(), "value": request.query["value"]})
        raise web.HTTPConflict(reason="original extension failure")

    app = web.Application()
    app.router.add_route("*", "/native/extension", extension)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
    transport = (HTTPTransport(url, token="control-token") if kind == "http" else
                 ZMQTransport("ipc:///unused-control-test.sock", token="control-token", management_url=url))
    try:
        await transport.open()
        assert await transport.native_request("POST", "/extension", params={"value": "query"}, json={"id": 4}) == {
            "body": {"id": 4}, "value": "query"}
        with pytest.raises(ClientResponseError) as error:
            await transport.native_request("GET", "/extension")
        assert error.value.status == 409 and error.value.message.startswith("original extension failure:")
        assert error.value.details["body_state"] == "invalid_json"
        assert calls == ["POST", "GET"]
    finally:
        await transport.close()
        await runner.cleanup()


async def test_zmq_control_requires_http_companion():
    transport = ZMQTransport("ipc:///unused-control-test.sock", token="secret")
    with pytest.raises(RemoteError) as error:
        await transport.native_request("POST", "/extension", json={})
    assert error.value.details["code"] == "UnsupportedTransport"
    await transport.close()


@pytest.mark.parametrize("case", ["string", "structured", "chunked", "empty", "invalid", "oversized", "long_message", "unauthorized"])
async def test_native_control_failure_preserves_bounded_plugin_error_and_never_retries_post(case):
    original = ("The saved film changed. Reload the scene before trimming." if case != "long_message"
                else "x" * 15000)
    plugin_error = ({"code": "RevisionChanged", "message": original, "details": {"expected": "r1", "actual": "r2"}}
                    if case == "structured" else original)
    payload = json.dumps({"error": plugin_error}).encode()
    status = 401 if case == "unauthorized" else 400
    if case == "empty":
        payload = b""
    elif case == "invalid":
        payload = b"<html>malformed plugin response</html>"
    elif case == "oversized":
        payload = json.dumps({"error": "must-not-include-this-body" * 3000}).encode()
    calls = []

    async def extension(request):
        calls.append((request.method, await request.json()))
        assert request.headers["Authorization"] == "Bearer do-not-print-token"
        if case == "chunked":
            response = web.StreamResponse(status=status, headers={"Content-Type": "application/json"})
            await response.prepare(request)
            for offset in range(0, len(payload), 9):
                await response.write(payload[offset:offset + 9])
                await asyncio.sleep(.002)
            await response.write_eof()
            return response
        return web.Response(status=status, body=payload, content_type="application/json")

    app = web.Application()
    app.router.add_post("/native/extension", extension)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
    transport = HTTPTransport(url, token="do-not-print-token")
    try:
        await transport.open()
        with pytest.raises(ClientResponseError) as caught:
            await transport.native_request("POST", "/extension", json={"revision": "r1"})
        error = caught.value
        assert error.status == status and error.details["status"] == status
        assert "do-not-print-token" not in str(error)
        assert "do-not-print-token" not in repr(error.details)
        assert "Authorization" not in repr(error.details)
        if case in {"string", "structured", "chunked", "unauthorized", "long_message"}:
            assert error.details["body_state"] == "json"
            assert error.details["error"] == plugin_error
            assert original[:100] in error.message
            if case == "long_message":
                assert len(error.message) < 2100 and error.details["message_truncated"] is True
                assert original not in error.message
        else:
            expected = {"empty": "empty", "invalid": "invalid_json", "oversized": "too_large"}[case]
            assert error.details["body_state"] == expected
            assert "error" not in error.details
            assert "native control error body" in error.message
            assert "must-not-include-this-body" not in str(error)
            assert len(error.message) < 200
        assert calls == [("POST", {"revision": "r1"})]
    finally:
        await transport.close()
        await runner.cleanup()
