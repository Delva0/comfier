"""Explicit object-fetch compression selection does not affect file streams."""

import pytest
from aiohttp import web

from comani import Client, HTTPTransport, InProcessTransport
from comani._runtime.http import create_app
from comani.protocol import LZ4_ACCEPT_HEADER, LZ4_CODEC

from .test_binary_fetch import FetchRuntime, serve


@pytest.mark.parametrize("mode", [None, "lz4", "gzip", "", 1])
def test_invalid_compression_is_rejected_before_connecting(mode):
    with pytest.raises(ValueError, match="fetch_compression"):
        HTTPTransport("http://unused", fetch_compression=mode)
    with pytest.raises(ValueError, match="fetch_compression"):
        Client("http://unused", fetch_compression=mode)


@pytest.mark.parametrize("mode", ["auto", "identity"])
def test_existing_transports_do_not_silently_swallow_compression_configuration(mode):
    for transport in (HTTPTransport("http://unused"), InProcessTransport(object())):
        with pytest.raises(TypeError, match="on HTTPTransport"):
            Client(transport=transport, fetch_compression=mode)


async def test_auto_and_identity_over_real_http_return_identical_values():
    pytest.importorskip("lz4.frame")
    runtime = FetchRuntime()
    seen = []

    @web.middleware
    async def observe(request, handler):
        if request.path == "/rpc":
            body = await request.json()
            if body.get("method") == "fetch":
                seen.append(request.headers.get(LZ4_ACCEPT_HEADER))
        return await handler(request)

    app = create_app(runtime)
    app.middlewares.append(observe)
    expected = {"bytes": b"x" * (2 * 1024 * 1024), "nested": [1, "中文"]}
    async with serve(app) as url:
        for mode in ("auto", "identity"):
            async with Client(url, fetch_compression=mode) as client:
                ref = runtime.store.own(client.session_id, [expected])
                assert await client._fetch(ref) == expected
    assert seen == [LZ4_CODEC, None]
