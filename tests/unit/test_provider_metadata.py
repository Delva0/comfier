"""Provider JSON must consume the complete response within a fixed byte budget."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from comfier.daemon.management.operations import ManagementError
from comfier.daemon.management.providers import ProviderResolver

LIMIT = 8 * 1024 * 1024


@asynccontextmanager
async def server(handler):
    app = web.Application()
    app.router.add_get("/metadata", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/metadata"
    finally:
        await runner.cleanup()


async def test_metadata_waits_for_delayed_json_chunks_until_eof():
    async def handler(request):
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b'{"items":[')
        await asyncio.sleep(0.1)
        await response.write(b'1,2,3],"name":"\xe6\xa8')
        await asyncio.sleep(0.01)
        await response.write(b'\xa1\xe5\x9e\x8b"}')
        await response.write_eof()
        return response

    async with server(handler) as url:
        assert await ProviderResolver().json(url) == {"items": [1, 2, 3], "name": "模型"}


async def test_metadata_accepts_exact_budget_response():
    async def handler(request):
        return web.Response(body=b"{}" + b" " * (LIMIT - 2))

    async with server(handler) as url:
        assert await ProviderResolver().json(url) == {}


async def test_metadata_rejects_over_budget_before_eof():
    release = asyncio.Event()

    async def handler(request):
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b"{}" + b" " * (LIMIT - 2))
        await asyncio.sleep(0.01)
        await response.write(b" ")
        # A size rejection must not wait for a server which keeps the body open.
        await release.wait()
        return response

    async with server(handler) as url:
        try:
            with pytest.raises(ManagementError, match="exceeds 8 MiB") as failure:
                await asyncio.wait_for(ProviderResolver().json(url), 2)
            assert failure.value.code == "ProviderError"
        finally:
            release.set()


@pytest.mark.parametrize("payload", [b"", b'{"items":', b'{"value":"\xff"}'])
async def test_invalid_metadata_is_a_sanitized_provider_error(payload):
    async def handler(request):
        return web.Response(body=payload)

    async with server(handler) as url:
        with pytest.raises(ManagementError) as failure:
            await ProviderResolver().json(url + "?token=private-value")
        assert failure.value.code == "ProviderError"
        assert "private-value" not in str(failure.value)


async def test_metadata_disconnect_is_a_sanitized_provider_error():
    async def handler(request):
        response = web.StreamResponse(headers={"Content-Length": "100"})
        await response.prepare(request)
        await response.write(b'{"items":[')
        request.transport.close()
        return response

    async with server(handler) as url:
        with pytest.raises(ManagementError) as failure:
            await ProviderResolver().json(url + "?token=private-value")
        assert failure.value.code == "ProviderError"
        assert "private-value" not in str(failure.value)


async def test_metadata_cancellation_closes_response_without_masking_cancel():
    started, release = asyncio.Event(), asyncio.Event()
    transport = None

    async def handler(request):
        nonlocal transport
        transport = request.transport
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b'{"items":[')
        started.set()
        await release.wait()
        return response

    async with server(handler) as url:
        task = asyncio.create_task(ProviderResolver().json(url))
        try:
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            for _ in range(100):
                if transport.is_closing():
                    break
                await asyncio.sleep(0.01)
            assert transport.is_closing()
        finally:
            release.set()
