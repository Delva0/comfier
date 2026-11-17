"""Optional codec negotiation and corruption boundaries over real HTTP."""

import gzip
import os

import cloudpickle
import pytest
from aiohttp import ClientPayloadError, ClientSession, web

from comfier import Client, RemoteError
from comfier._codec import LZ4_MIN_BYTES, decompress_pickle
from comfier.daemon.http import create_app
from comfier.protocol import (
    LZ4_ACCEPT_HEADER,
    LZ4_CODEC,
    LZ4_PICKLE_CONTENT_TYPE,
    PICKLE_CONTENT_TYPE,
    PICKLE_LENGTH_HEADER,
    unpack_pickle,
)

from .test_binary_fetch import FetchRuntime, negotiated_transport, serve

CAPABILITIES = {"binary_fetch": 1, "binary_fetch_lz4": 1}


@pytest.fixture
def lz4_frame():
    return pytest.importorskip("lz4.frame")


async def test_missing_optional_server_codec_keeps_handshake_and_fetch_working(monkeypatch):
    monkeypatch.setattr("comfier._codec._lz4", None)
    runtime = FetchRuntime()
    async with serve(create_app(runtime)) as url:
        async with Client(url) as client:
            assert "binary_fetch_lz4" not in client.handshake["capabilities"]
            ref = runtime.store.own(client.session_id, [b"a" * LZ4_MIN_BYTES])
            assert await client._fetch(ref) == b"a" * LZ4_MIN_BYTES
            params = {"session_id": client.session_id, "ref_id": ref["id"], "epoch": ref["epoch"]}
            async with ClientSession() as http:
                async with http.post(url + "/rpc", json={"method": "fetch", "params": params},
                                     headers={"Accept": PICKLE_CONTENT_TYPE,
                                              LZ4_ACCEPT_HEADER: LZ4_CODEC}) as response:
                    assert response.content_type == PICKLE_CONTENT_TYPE
                    assert unpack_pickle(await response.read()) == b"a" * LZ4_MIN_BYTES


@pytest.mark.parametrize("capabilities", [
    {"binary_fetch": 1}, {"binary_fetch": 1, "binary_fetch_lz4": True},
    {"binary_fetch": 1, "binary_fetch_lz4": "1"},
    {"binary_fetch": 1, "binary_fetch_lz4": 2}, {"binary_fetch_lz4": 1},
])
async def test_unknown_codec_or_missing_binary_capability_uses_identity(capabilities):
    requests = []

    async def fetch(request):
        requests.append(request.headers.get(LZ4_ACCEPT_HEADER))
        if request.headers.get("Accept") == PICKLE_CONTENT_TYPE:
            return web.Response(body=cloudpickle.dumps(123), content_type=PICKLE_CONTENT_TYPE)
        from comfier.protocol import pack_pickle

        return web.json_response({"result": pack_pickle(123)})

    async with negotiated_transport(fetch, capabilities) as transport:
        assert unpack_pickle(await transport.request("fetch")) == 123
    assert requests == [None]


async def test_missing_optional_client_codec_does_not_request_lz4(monkeypatch):
    monkeypatch.setattr("comfier._codec._lz4", None)
    requests = []

    async def fetch(request):
        requests.append(request.headers.get(LZ4_ACCEPT_HEADER))
        return web.Response(body=cloudpickle.dumps("identity"), content_type=PICKLE_CONTENT_TYPE)

    async with negotiated_transport(fetch, CAPABILITIES) as transport:
        assert unpack_pickle(await transport.request("fetch")) == "identity"
    assert requests == [None]


async def test_large_generic_value_uses_lz4_and_legacy_binary_client_keeps_identity(lz4_frame):
    runtime = FetchRuntime()
    shared = [b"repeated" * LZ4_MIN_BYTES]
    shared.append(shared)
    value = {"left": shared, "right": shared, "call": lambda x: x + 2}
    media_types = []
    app = create_app(runtime)

    async def prepared(request, response):
        if request.path == "/rpc" and (await request.json()).get("method") == "fetch":
            media_types.append(response.content_type)

    app.on_response_prepare.append(prepared)
    async with serve(app) as url:
        async with Client(url) as client:
            ref = runtime.store.own(client.session_id, [value])
            result = await client._fetch(ref)
            assert result["left"] is result["right"]
            assert result["left"][1] is result["left"]
            assert result["left"][0] == shared[0] and result["call"](4) == 6
            assert media_types == [LZ4_PICKLE_CONTENT_TYPE]
            params = {"session_id": client.session_id, "ref_id": ref["id"], "epoch": ref["epoch"]}
            async with ClientSession() as http:
                for codec in (None, "lz4-frame-v2"):
                    headers = {"Accept": PICKLE_CONTENT_TYPE}
                    if codec:
                        headers[LZ4_ACCEPT_HEADER] = codec
                    async with http.post(url + "/rpc", json={"method": "fetch", "params": params},
                                         headers=headers) as response:
                        assert response.content_type == PICKLE_CONTENT_TYPE
                        assert PICKLE_LENGTH_HEADER not in response.headers
                        assert unpack_pickle(await response.read())["call"](4) == 6


@pytest.mark.parametrize("kind", ["small", "entropy", "already-compressed"])
async def test_small_or_incompressible_fetch_returns_same_request_identity(lz4_frame, kind):
    runtime = FetchRuntime()
    value = b"small" if kind == "small" else os.urandom(LZ4_MIN_BYTES)
    if kind == "already-compressed":
        value = gzip.compress(value)
    media_types = []
    app = create_app(runtime)

    async def prepared(request, response):
        if request.path == "/rpc" and (await request.json()).get("method") == "fetch":
            media_types.append(response.content_type)

    app.on_response_prepare.append(prepared)
    async with serve(app) as url:
        async with Client(url) as client:
            ref = runtime.store.own(client.session_id, [value])
            assert await client._fetch(ref) == value
            assert media_types == [PICKLE_CONTENT_TYPE]
            assert runtime.fetches == 1


@pytest.mark.parametrize("inner", ["identity", "lz4"])
@pytest.mark.parametrize("outer", ["identity", "gzip"])
async def test_http_content_encoding_is_independent_of_comani_codec(lz4_frame, inner, outer):
    raw = cloudpickle.dumps({"value": b"abc" * 1000})
    body = lz4_frame.compress(raw, content_checksum=True) if inner == "lz4" else raw
    headers = {PICKLE_LENGTH_HEADER: str(len(raw))} if inner == "lz4" else {}
    media_type = LZ4_PICKLE_CONTENT_TYPE if inner == "lz4" else PICKLE_CONTENT_TYPE
    if outer == "gzip":
        body = gzip.compress(body)
        headers["Content-Encoding"] = "gzip"
    requests = []

    async def fetch(request):
        requests.append(request.headers.get(LZ4_ACCEPT_HEADER))
        return web.Response(body=body, content_type=media_type, headers=headers)

    async with negotiated_transport(fetch, CAPABILITIES) as transport:
        assert await transport.request("fetch") == raw
    assert requests == [LZ4_CODEC]


@pytest.mark.parametrize("damage", [
    "missing-length", "negative-length", "huge-length", "zero-length", "wrong-length",
    "truncated", "bitflip", "trailing", "second-frame", "no-checksum", "no-content-size",
])
async def test_invalid_lz4_response_fails_once_without_fallback(lz4_frame, damage):
    raw = cloudpickle.dumps(b"abcdefgh" * 1000)
    frame = lz4_frame.compress(raw, content_checksum=True)
    length = str(len(raw))
    if damage == "missing-length":
        length = None
    elif damage == "negative-length":
        length = "-1"
    elif damage == "huge-length":
        length = "9" * 21
    elif damage == "zero-length":
        length = "0"
    elif damage == "wrong-length":
        length = str(len(raw) + 1)
    elif damage == "truncated":
        frame = frame[:-1]
    elif damage == "bitflip":
        frame = frame[:-1] + bytes([frame[-1] ^ 1])
    elif damage == "trailing":
        frame += b"trailing"
    elif damage == "second-frame":
        frame += frame
    elif damage == "no-checksum":
        frame = lz4_frame.compress(raw, content_checksum=False)
    elif damage == "no-content-size":
        frame = lz4_frame.compress(raw, content_checksum=True, store_size=False)
    requests = []

    async def fetch(request):
        requests.append(request.headers[LZ4_ACCEPT_HEADER])
        headers = {PICKLE_LENGTH_HEADER: length} if length is not None else {}
        return web.Response(body=frame, content_type=LZ4_PICKLE_CONTENT_TYPE, headers=headers)

    async with negotiated_transport(fetch, CAPABILITIES) as transport:
        with pytest.raises(RemoteError, match="LZ4"):
            await transport.request("fetch")
    assert requests == [LZ4_CODEC]


async def test_unnegotiated_lz4_response_is_an_error_without_retry(lz4_frame):
    raw = cloudpickle.dumps(123)
    requests = []

    async def fetch(request):
        requests.append(request.headers.get(LZ4_ACCEPT_HEADER))
        return web.Response(body=lz4_frame.compress(raw, content_checksum=True),
                            content_type=LZ4_PICKLE_CONTENT_TYPE,
                            headers={PICKLE_LENGTH_HEADER: str(len(raw))})

    async with negotiated_transport(fetch, {"binary_fetch": 1}) as transport:
        with pytest.raises(RemoteError, match="negotiation"):
            await transport.request("fetch")
    assert requests == [None]


async def test_truncated_http_entity_stays_a_payload_error_before_lz4_decode(lz4_frame):
    requests = []

    async def fetch(request):
        requests.append(request.headers[LZ4_ACCEPT_HEADER])
        response = web.StreamResponse(headers={
            "Content-Type": LZ4_PICKLE_CONTENT_TYPE, "Content-Length": "1000",
            PICKLE_LENGTH_HEADER: "2000",
        })
        await response.prepare(request)
        await response.write(b"truncated")
        request.transport.close()
        return response

    async with negotiated_transport(fetch, CAPABILITIES) as transport:
        with pytest.raises(ClientPayloadError):
            await transport.request("fetch")
    assert requests == [LZ4_CODEC]


def test_missing_decoder_is_an_explicit_error(monkeypatch):
    monkeypatch.setattr("comfier._codec._lz4", None)
    with pytest.raises(RemoteError, match="local decoder"):
        decompress_pickle(b"not decoded", "10")
