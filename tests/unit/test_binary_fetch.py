"""Binary HTTP fetch keeps the existing cloudpickle and error boundaries."""

import asyncio
import gc
import pickle
import weakref
from contextlib import asynccontextmanager
from types import SimpleNamespace

import cloudpickle
import pytest
from aiohttp import ClientPayloadError, ClientResponseError, ClientSession, web
from aiohttp.test_utils import TestServer

from comani import Client, Expired, HTTPTransport, InProcessTransport, RemoteError, SessionClosed
from comani.daemon.http import create_app
from comani.daemon.service import NativeRuntime
from comani.protocol import BINARY_FETCH_VERSION, PICKLE_CONTENT_TYPE, pack_pickle, unpack_pickle


class FetchRuntime(NativeRuntime):
    """Exercise real sessions/object lookup/serialization without booting ComfyUI."""

    def __init__(self):
        super().__init__("/unused")
        self.adapter.node_types = lambda: {}
        self.fetches = 0

    async def start(self):
        return self

    async def fetch_bytes(self, *args, **kwargs):
        self.fetches += 1
        return await super().fetch_bytes(*args, **kwargs)


@asynccontextmanager
async def serve(app):
    async with TestServer(app) as server:
        yield str(server.make_url("/")).rstrip("/")


@asynccontextmanager
async def negotiated_transport(fetch, capabilities=None):
    async def rpc(request):
        body = await request.json()
        if body["method"] == "handshake":
            return web.json_response({"result": {"capabilities": capabilities}})
        return await fetch(request)

    app = web.Application()
    app.router.add_post("/rpc", rpc)
    async with serve(app) as url:
        transport = HTTPTransport(url)
        await transport.open()
        try:
            await transport.request("handshake")
            yield transport
        finally:
            await transport.close()


async def test_sdk_binary_fetch_and_legacy_json_preserve_cloudpickle_values():
    runtime = FetchRuntime()
    shared = [1, "中文", bytes(range(256))]
    shared.append(shared)
    value = {"left": shared, "right": shared, "function": lambda x: x + 3}
    async with serve(create_app(runtime)) as url:
        async with Client(url) as client:
            assert client.handshake["capabilities"]["binary_fetch"] == BINARY_FETCH_VERSION
            ref = runtime.store.own(client.session_id, [value])
            result = await client._fetch(ref)
            assert result["left"] is result["right"]
            assert result["left"][3] is result["left"]
            assert result["left"][:3] == shared[:3]
            assert result["function"](4) == 7
            params = {"session_id": client.session_id, "ref_id": ref["id"], "epoch": ref["epoch"]}
            async with ClientSession() as http:
                for headers in ({}, {"Accept": PICKLE_CONTENT_TYPE}):
                    async with http.post(url + "/rpc", json={"method": "fetch", "params": params},
                                         headers=headers) as response:
                        body = await response.read()
                        assert response.content_length == len(body)
                        assert "Content-Encoding" not in response.headers
                        if headers:
                            assert response.content_type == PICKLE_CONTENT_TYPE
                            decoded = unpack_pickle(body)
                        else:
                            assert response.content_type == "application/json"
                            decoded = unpack_pickle((await response.json())["result"])
                        assert decoded["function"](5) == 8
                        assert decoded["left"] is decoded["right"]
            assert runtime.fetches == 3


@pytest.mark.parametrize("capabilities", [None, {}, [], {"binary_fetch": 2},
                                        {"binary_fetch": "1"}, {"binary_fetch": True}])
async def test_old_or_unknown_server_capability_uses_one_legacy_fetch(capabilities):
    requests = []

    async def fetch(request):
        requests.append(request.headers.get("Accept"))
        return web.json_response({"result": pack_pickle({"legacy": True})})

    async with negotiated_transport(fetch, capabilities) as transport:
        assert unpack_pickle(await transport.request("fetch")) == {"legacy": True}
    assert requests == ["*/*"]


async def test_negotiated_fetch_can_use_same_response_json_without_retry():
    requests = []

    async def fetch(request):
        requests.append(request.headers["Accept"])
        return web.json_response({"result": pack_pickle("legacy response")})

    async with negotiated_transport(fetch, {"binary_fetch": 1}) as transport:
        assert unpack_pickle(await transport.request("fetch")) == "legacy response"
    assert requests == [PICKLE_CONTENT_TYPE]


async def test_binary_object_containing_error_is_data_not_an_rpc_error():
    value = {"error": {"code": "SessionClosed", "message": "ordinary user data"}}

    async def fetch(request):
        return web.Response(body=cloudpickle.dumps(value), content_type=PICKLE_CONTENT_TYPE)

    async with negotiated_transport(fetch, {"binary_fetch": 1}) as transport:
        assert unpack_pickle(await transport.request("fetch")) == value


async def test_binary_fetch_preserves_runtime_auth_session_and_reference_errors():
    runtime = FetchRuntime()
    async with serve(create_app(runtime, token="fixture-token")) as url:
        transport = HTTPTransport(url, token="fixture-token")
        await transport.open()
        try:
            handshake = await transport.request("handshake")
            session, epoch = handshake["session_id"], handshake["epoch"]
            ref = runtime.store.own(session, [123])
            params = {"session_id": session, "ref_id": ref["id"], "epoch": epoch}
            runtime.store.release(session, [ref["id"]])
            with pytest.raises(Expired):
                await transport.request("fetch", **params)
            with pytest.raises(Expired):
                await transport.request("fetch", **{**params, "epoch": "other-epoch"})
            await runtime.close_session(session)
            with pytest.raises(SessionClosed):
                await transport.request("fetch", **params)
            assert runtime.fetches == 3
            transport._data_session.headers["Authorization"] = "Bearer invalid"
            with pytest.raises(RemoteError) as caught:
                await transport.request("fetch", **params)
            assert caught.value.details["code"] == "Unauthorized"
            assert runtime.fetches == 3
        finally:
            await transport.close()


@pytest.mark.parametrize("status, content_type, body, error", [
    (503, "text/html", b"unavailable", ClientResponseError),
    (503, PICKLE_CONTENT_TYPE, cloudpickle.dumps("unavailable"), ClientResponseError),
    (200, "application/octet-stream", cloudpickle.dumps(123), RemoteError),
    (200, "application/json", b"{invalid", RemoteError),
])
async def test_binary_fetch_http_and_invalid_response_errors_are_not_retried(
    status, content_type, body, error,
):
    requests = []

    async def fetch(request):
        requests.append(request.headers["Accept"])
        return web.Response(status=status, body=body, content_type=content_type)

    async with negotiated_transport(fetch, {"binary_fetch": 1}) as transport:
        with pytest.raises(error):
            await transport.request("fetch")
    assert requests == [PICKLE_CONTENT_TYPE]


async def test_binary_fetch_truncated_body_keeps_payload_error_without_retry():
    requests = []

    async def fetch(request):
        requests.append(request.headers["Accept"])
        response = web.StreamResponse(headers={
            "Content-Type": PICKLE_CONTENT_TYPE, "Content-Length": "1000",
        })
        await response.prepare(request)
        await response.write(b"truncated")
        request.transport.close()
        return response

    async with negotiated_transport(fetch, {"binary_fetch": 1}) as transport:
        with pytest.raises(ClientPayloadError):
            await transport.request("fetch")
    assert requests == [PICKLE_CONTENT_TYPE]


async def test_binary_fetch_cancellation_is_not_retried():
    started, finish = asyncio.Event(), asyncio.Event()
    requests = []

    async def fetch(request):
        requests.append(request.headers["Accept"])
        response = web.StreamResponse(headers={"Content-Type": PICKLE_CONTENT_TYPE})
        await response.prepare(request)
        started.set()
        await finish.wait()
        return response

    async with negotiated_transport(fetch, {"binary_fetch": 1}) as transport:
        task = asyncio.create_task(transport.request("fetch"))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert requests == [PICKLE_CONTENT_TYPE]
        finally:
            finish.set()


class Source:
    pass


class Cache:
    pass


@pytest.mark.parametrize("ownership", ["owned", "borrowed"])
async def test_serialization_holds_source_locally_and_does_not_add_a_cache(monkeypatch, ownership):
    runtime = FetchRuntime()
    try:
        session = (await runtime.handshake())["session_id"]
        source = Source()
        source.value = "still alive while serializing"
        source_ref = weakref.ref(source)
        if ownership == "owned":
            ref = runtime.store.own(session, [source])

            def evict():
                runtime.store.release(session, [ref["id"]])
        else:
            cache = Cache()
            cache._comani_entry_versions = {"key": "version"}
            cache.cache = {"key": SimpleNamespace(outputs=[[source]])}
            ref = runtime.store.borrow(session, cache, "key", cache.cache["key"], 0)

            def evict():
                cache.cache.clear()
        del source
        original = cloudpickle.dumps

        def serialize(value):
            evict()
            gc.collect()
            assert source_ref() is value
            return original(value)

        with monkeypatch.context() as patch:
            patch.setattr("comani.daemon.service.cloudpickle.dumps", serialize)
            payload = await runtime.fetch_bytes(session, ref["id"], runtime.epoch)
        assert unpack_pickle(payload).value == "still alive while serializing"
        gc.collect()
        assert source_ref() is None
        with pytest.raises(Expired):
            await runtime.fetch_bytes(session, ref["id"], runtime.epoch)
    finally:
        await runtime.close()


async def test_inprocess_and_custom_transports_keep_existing_fetch_envelopes(peer):
    client, runtime = peer
    ref = await client.node("Echo", return_call=True, value="existing envelope").output()
    assert await ref.fetch() == "existing envelope"
    assert isinstance(client.transport, InProcessTransport)
    assert isinstance(await client.transport.request("fetch", session_id=client.session_id,
                                                   ref_id=ref.id, epoch=runtime.epoch), dict)


async def test_binary_unpickle_failure_is_not_interpreted_as_json():
    requests = []

    async def fetch(request):
        requests.append(request.headers["Accept"])
        return web.Response(body=b'{"error": {"code": "SessionClosed"}}',
                            content_type=PICKLE_CONTENT_TYPE)

    async with negotiated_transport(fetch, {"binary_fetch": 1}) as transport:
        with pytest.raises(pickle.UnpicklingError):
            unpack_pickle(await transport.request("fetch"))
    assert requests == [PICKLE_CONTENT_TYPE]
