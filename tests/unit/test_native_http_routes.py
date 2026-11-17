from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, web

from comfier.daemon.http import create_runtime_app


class Runtime:
    def __init__(self, *, fail=False):
        self.fail, self.started, self.closed = fail, False, False
        self.requests, self.listeners, self.calls = {}, set(), {}

    async def start(self):
        if self.fail:
            raise RuntimeError("boot failed")
        if self.started:
            return
        self.started = True
        native, routes = web.Application(), web.RouteTableDef()

        @routes.post("/plugin/{item}")
        async def control(request):
            assert request.app is native
            return web.json_response({"item": request.match_info["item"],
                                      "query": dict(request.query), "body": await request.json()})

        @routes.get("/plugin/error")
        async def error(request):
            return web.json_response({"error": "original error"}, status=400)

        @routes.route("*", "/{anything:.*}")
        async def broad_plugin(request):
            return web.json_response({"native": True})

        self.adapter = SimpleNamespace(server=SimpleNamespace(app=native, routes=routes))

    async def close(self):
        self.closed = True

    async def handshake(self):
        return {"owned": True}


async def test_native_subapp_keeps_auth_request_context_and_comani_routes():
    runtime = Runtime()
    runner = web.AppRunner(await create_runtime_app(runtime, token="secret"))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        async with ClientSession() as client:
            async with client.get(url + "/native/plugin/error") as response:
                assert response.status == 401
        async with ClientSession(headers={"Authorization": "Bearer secret"}) as client:
            async with client.post(url + "/native/plugin/chair", params={"version": "a&b"},
                                   json={"trim": [3, 80]}) as response:
                assert response.status == 200
                assert await response.json() == {"item": "chair", "query": {"version": "a&b"},
                                                  "body": {"trim": [3, 80]}}
            async with client.get(url + "/native/plugin/error") as response:
                assert response.status == 400
                assert await response.json() == {"error": "original error"}
            for method in ("GET", "POST", "DELETE"):
                for path in ("prompt", "queue", "history/take", "files/item", "api/jobs/abc"):
                    async with client.request(method, url + "/native/" + path) as response:
                        assert response.status == 404
            async with client.post(url + "/rpc", json={"method": "handshake"}) as response:
                assert await response.json() == {"result": {"owned": True}}
            async with client.get(url + "/history/a") as response:
                assert await response.json() == {}
    finally:
        await runner.cleanup()
    assert runtime.closed


async def test_native_boot_failure_closes_runtime():
    runtime = Runtime(fail=True)
    with pytest.raises(RuntimeError, match="boot failed"):
        await create_runtime_app(runtime)
    assert runtime.closed
