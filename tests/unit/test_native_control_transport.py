"""Extension JSON requests retain the shared authenticated HTTP connection."""

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
        assert error.value.status == 409 and error.value.message == "original extension failure"
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
