import asyncio

from aiohttp import ClientSession, web

from comani._runtime.http import create_app


class Runtime:
    def __init__(self):
        self.requests = {}
        self.listeners = set()

    async def start(self):
        pass

    async def close(self):
        pass

    async def handshake(self):
        return {"ok": True}


async def test_loopback_rpc_rejects_cross_site_and_rebinding_host():
    runner = web.AppRunner(create_app(Runtime()))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/rpc"
    try:
        async with ClientSession() as client:
            async with client.post(url, json={"method": "handshake"}) as response:
                assert response.status == 200
            async with client.post(url, data='{"method":"handshake"}', headers={"Content-Type": "text/plain", "Origin": "https://foreign.example"}) as response:
                assert response.status == 403
            async with client.post(url, json={"method": "handshake"}, headers={"Host": "foreign.example", "Origin": "http://foreign.example"}) as response:
                assert response.status == 403
            async with client.post(url, json={"method": "handshake"}, headers={"Origin": f"http://127.0.0.1:{port}"}) as response:
                assert response.status == 200
    finally:
        await runner.cleanup()


async def test_configured_token_is_required_for_rpc():
    runner = web.AppRunner(create_app(Runtime(), token="test-token"))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/rpc"
    try:
        async with ClientSession() as client:
            async with client.post(url, json={"method": "handshake"}) as response:
                assert response.status == 401
            async with client.post(url, json={"method": "handshake"}, headers={"Authorization": "Bearer test-token"}) as response:
                assert response.status == 200
    finally:
        await runner.cleanup()


async def test_websocket_disconnect_reclaims_subscription_tasks():
    runtime = Runtime()
    runner = web.AppRunner(create_app(runtime))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/ws"
    try:
        async with ClientSession() as client:
            socket = await client.ws_connect(url)
            await asyncio.sleep(0)
            assert runtime.listeners
            await socket.close()
            for _ in range(20):
                if not runtime.listeners and not any(task.get_coro().__qualname__ == "Queue.get" for task in asyncio.all_tasks()):
                    break
                await asyncio.sleep(0.01)
            assert not runtime.listeners
            assert not any(task.get_coro().__qualname__ == "Queue.get" for task in asyncio.all_tasks())
    finally:
        await runner.cleanup()
