"""Real HTTP lease traffic, served on a separate thread from the SDK loop."""

import asyncio
import gc
import threading
import time
import uuid
import weakref
from concurrent.futures import Future

import pytest
from aiohttp import ClientConnectionError, web

from comani import Client, HTTPTransport, InProcessTransport, RemoteError, SessionClosed
from comani.protocol import pack_pickle
from comani.transport import _post


async def until(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert predicate()


class LeaseServer:
    ttl = 0.3

    def __init__(self):
        self.ready = Future()
        self.seen = {}
        self.heartbeats = self.handshakes = self.submissions = self.closes = self.fetches = 0
        self.block_results = self.drop_heartbeats = self.omit_session_id = False
        self.drop_fetches = False
        self.heartbeat_response = None
        self.heartbeat_rejections = 0
        self.thread = threading.Thread(target=lambda: asyncio.run(self.serve()), daemon=True)
        self.thread.start()
        self.url = self.ready.result(timeout=3)

    async def rpc(self, request):
        body = await request.json()
        method, params = body["method"], body["params"]
        if method == "fetch":
            self.fetches += 1
        session = params.get("session_id")
        if method == "handshake":
            self.handshakes += 1
            session = uuid.uuid4().hex
            self.seen[session] = time.monotonic()
            result = {"session_id": session, "epoch": "lease-test", "session_ttl": self.ttl,
                      "nodes": {"Echo": {"outputs": ["*"] * getattr(self, "output_count", 1)}}}
            if self.omit_session_id:
                result.pop("session_id")
        elif method == "close_session":
            self.closes += 1
            self.seen.pop(session, None)
            self.gate.set()
            result = {}
        elif method == "heartbeat" and self.drop_heartbeats:
            request.transport.close()
            return web.Response()
        elif method == "heartbeat" and self.heartbeat_response is not None:
            self.heartbeat_rejections += 1
            status, payload = self.heartbeat_response
            if isinstance(payload, tuple) and payload[0] == "json":
                return web.json_response(payload[1], status=status)
            if isinstance(payload, dict):
                return web.json_response(payload, status=status)
            if isinstance(payload, bytes):
                response = web.StreamResponse(status=status, headers={
                    "Content-Type": "application/json", "Content-Length": str(len(payload) + 100),
                })
                await response.prepare(request)
                await response.write(payload)
                request.transport.close()
                return response
            return web.Response(text=payload, status=status)
        elif method == "fetch" and self.drop_fetches:
            request.transport.close()
            return web.Response()
        elif session not in self.seen or time.monotonic() - self.seen[session] > self.ttl:
            self.seen.pop(session, None)
            return web.json_response({"error": {"code": "SessionClosed", "message": "lease expired"}})
        else:
            self.seen[session] = time.monotonic()
            result = {}
            if method == "heartbeat":
                self.heartbeats += 1
            elif method == "submit":
                self.submissions += 1
            elif method == "wait_result":
                if self.block_results:
                    await self.gate.wait()
                result = {"outputs": [{"id": "value", "epoch": "lease-test", "owner": "owned"}]}
            elif method == "fetch":
                result = pack_pickle(12345)
        return web.json_response({"result": result})

    async def serve(self):
        self.loop = asyncio.get_running_loop()
        self.stopped, self.gate = asyncio.Event(), asyncio.Event()
        app = web.Application()
        app.router.add_post("/rpc", self.rpc)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.ready.set_result(f"http://127.0.0.1:{port}")
        try:
            await self.stopped.wait()
        finally:
            self.gate.set()
            await runner.cleanup()

    def close(self):
        self.loop.call_soon_threadsafe(self.stopped.set)
        self.thread.join(timeout=3)
        assert not self.thread.is_alive()


@pytest.fixture
def lease_server():
    server = LeaseServer()
    yield server
    server.close()


async def test_http_lease_survives_blocked_business_loop_and_close_is_idempotent(lease_server):
    client = await Client(lease_server.url).start()
    lease = client._lease
    try:
        ref = await client.node("Echo", return_call=True).output()
        assert await ref.fetch() == 12345
        before = lease_server.heartbeats
        time.sleep(lease_server.ttl * 3)
        assert lease_server.heartbeats > before
        assert await ref.fetch() == 12345
        assert lease_server.submissions == lease_server.handshakes == 1
        await asyncio.gather(client.close(), client.close())
        assert lease.done.done() and not lease._thread.is_alive()
        assert lease_server.closes == 1
    finally:
        await client.close(wait=False)


async def test_dropped_http_client_without_public_handles_stops_lease(lease_server):
    client = await Client(lease_server.url).start()
    lease, transport = client._lease, client.transport
    client_ref = weakref.ref(client)
    del client
    gc.collect()
    await until(lambda: client_ref() is None)
    await until(lambda: lease.done.done() and transport._session is None)
    assert not lease._thread.is_alive()
    assert lease_server.closes == 1


async def test_cancelled_open_after_lease_start_stops_thread_and_connections(lease_server, monkeypatch):
    transport = HTTPTransport(lease_server.url)
    original = transport._start_heartbeat
    started = []

    def delayed_ready(*args):
        lease = original(*args)
        lease.ready.result(timeout=3)
        lease.ready = Future()  # Hold open after the actual thread has started.
        started.append(lease)
        return lease

    monkeypatch.setattr(transport, "_start_heartbeat", delayed_ready)
    client = Client(transport=transport)
    task = asyncio.create_task(client.start())
    await until(lambda: bool(started) and lease_server.heartbeats > 0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.closed and started[0].done.done()
    assert not started[0]._thread.is_alive() and transport._session is None
    assert lease_server.closes == 1


async def test_terminal_heartbeat_error_stops_without_reopening_or_resubmitting(lease_server):
    client = await Client(lease_server.url).start()
    lease = client._lease
    try:
        lease_server.drop_heartbeats = True
        await until(lambda: isinstance(lease.error, RemoteError))
        assert not client.closed  # The server, rather than a local timer, owns expiry.
        await asyncio.sleep(lease_server.ttl * 2)
        assert not client.closed
        lease_server.drop_heartbeats = False
        await until(lambda: client.closed and lease.done.done())
        with pytest.raises(SessionClosed, match="lease expired"):
            client.node("Echo", return_call=True)
        assert lease_server.handshakes == 1 and lease_server.submissions == 0
        await until(lambda: client.transport._session is None)
    finally:
        await client.close(wait=False)


async def test_heartbeat_thread_exits_when_owner_loop_is_closed(lease_server):
    owner_loop = asyncio.new_event_loop()
    transport = HTTPTransport(lease_server.url)
    await transport.open()
    session = (await transport.request("handshake"))["session_id"]
    lease = transport._start_heartbeat(session, 0.1, owner_loop, lambda *args: None)
    try:
        await asyncio.wrap_future(lease.ready)
        owner_loop.close()
        await until(lambda: lease.done.done())
        await lease.close()
        assert not lease._thread.is_alive()
    finally:
        owner_loop.close()
        await lease.close()
        await transport.close()


async def test_http_handshake_requires_server_session_before_starting_heartbeat(lease_server):
    lease_server.omit_session_id = True
    client = Client(lease_server.url)
    with pytest.raises(RemoteError, match="server-issued session_id"):
        await client.start()
    assert client.closed and client._lease is None and client.transport._session is None
    assert lease_server.heartbeats == 0 and lease_server.handshakes == 1


async def test_custom_transport_keeps_close_callback_without_handshake_session_id():
    closed = []

    class Runtime:
        async def handshake(self, **params):
            return {"epoch": "legacy-test-adapter"}

        async def close_session(self, session_id):
            closed.append(session_id)

    client = await Client(transport=InProcessTransport(Runtime())).start()
    await client.close()
    assert closed == [client.session_id]


@pytest.mark.parametrize("failure", [TimeoutError, ClientConnectionError])
async def test_transient_heartbeat_failure_allows_business_rpcs_and_recovers(
        lease_server, monkeypatch, failure):
    failing = threading.Event()

    async def temporary_failure(session, url, method, params, **kwargs):
        if method == "heartbeat" and failing.is_set():
            raise failure("injected transient heartbeat failure")
        return await _post(session, url, method, params, **kwargs)

    monkeypatch.setattr("comani.transport._post", temporary_failure)
    client = await Client(lease_server.url).start()
    lease = client._lease
    try:
        ref = await client.node("Echo", return_call=True).output()
        assert await ref.fetch() == 12345
        failing.set()
        await until(lambda: isinstance(lease.error, RemoteError))
        assert not client.closed and not lease.terminal
        before = lease_server.fetches
        assert await ref.fetch() == 12345
        assert lease_server.fetches == before + 1
        assert failure.__name__ in str(lease.error)
        second = await client.node("Echo", return_call=True).output()
        assert await second.fetch() == 12345
        assert lease_server.submissions == 2 and lease_server.handshakes == 1
        # A real business connection failure must still reach the caller.
        lease_server.drop_fetches = True
        with pytest.raises(ClientConnectionError):
            await ref.fetch()
        lease_server.drop_fetches = False
        before = lease_server.heartbeats
        failing.clear()
        await until(lambda: lease.error is None and lease_server.heartbeats > before)
        assert await ref.fetch() == 12345
    finally:
        await client.close(wait=False)
    assert lease.done.done() and not lease._thread.is_alive()


async def test_transient_keeper_error_does_not_mask_actual_expired_session_rpc(lease_server):
    client = await Client(lease_server.url).start()
    lease = client._lease
    try:
        ref = await client.node("Echo", return_call=True).output()
        lease_server.drop_heartbeats = True
        await until(lambda: isinstance(lease.error, RemoteError))
        await asyncio.sleep(lease_server.ttl * 2)
        assert not client.closed and not lease.terminal
        before = lease_server.fetches
        with pytest.raises(SessionClosed, match="lease expired"):
            await ref.fetch()
        assert lease_server.fetches == before + 1
        assert lease_server.handshakes == lease_server.submissions == 1
    finally:
        await client.close(wait=False)


@pytest.mark.parametrize(("status", "payload"), [
    (503, "temporary overload"),
    (429, {"error": {"code": "TooManyRequests", "message": "retry later"}}),
    (408, "request timeout"),
    (502, {"error": {"code": "BadGateway", "message": "temporary gateway failure"}}),
    pytest.param(503, ("json", None), id="503-json-null"),
    pytest.param(503, ("json", []), id="503-json-list"),
    pytest.param(503, ("json", "maintenance"), id="503-json-string"),
    pytest.param(200, b'{"result":', id="truncated-body"),
])
async def test_temporary_heartbeat_http_status_preserves_business_and_recovers(
        lease_server, status, payload):
    client = await Client(lease_server.url).start()
    lease = client._lease
    try:
        ref = await client.node("Echo", return_call=True).output()
        lease_server.heartbeat_response = status, payload
        await until(lambda: lease_server.heartbeat_rejections > 0 and lease.error is not None)
        assert not lease.terminal and not client.closed
        if status == 200:
            assert "ClientPayloadError" in str(lease.error)
        else:
            assert lease.error.details["status"] == status
        before = lease_server.fetches
        assert await ref.fetch() == 12345
        assert lease_server.fetches == before + 1
        next_ref = await client.node("Echo", return_call=True).output()
        assert await next_ref.fetch() == 12345
        assert lease_server.handshakes == 1 and lease_server.submissions == 2
        before = lease_server.heartbeats
        lease_server.heartbeat_response = None
        await until(lambda: lease.error is None and lease_server.heartbeats > before)
        assert await ref.fetch() == 12345
    finally:
        await client.close(wait=False)
    assert lease.done.done() and not lease._thread.is_alive()


@pytest.mark.parametrize(("status", "payload", "details", "message"), [
    (401, "denied", {"status": 401}, "HTTP 401"),
    (403, {"error": {"code": "Unauthorized", "message": "token rejected"}},
     {"code": "Unauthorized"}, "token rejected"),
    (503, {"error": {"code": "SessionClosed", "message": "server closed session"}},
     {"code": "SessionClosed"}, "server closed session"),
    (200, "invalid JSON", {}, "invalid JSON response"),
    pytest.param(200, ("json", None), {"status": 200}, "non-object RPC response", id="200-json-null"),
    pytest.param(200, ("json", []), {"status": 200}, "non-object RPC response", id="200-json-list"),
    pytest.param(200, ("json", "maintenance"), {"status": 200}, "non-object RPC response", id="200-json-string"),
])
async def test_permanent_heartbeat_errors_keep_terminal_details_and_fail_pending_calls(
        lease_server, status, payload, details, message):
    lease_server.block_results = True
    client = await Client(lease_server.url).start()
    lease = client._lease
    try:
        call = client.node("Echo", return_call=True)
        await until(lambda: lease_server.submissions == 1)
        lease_server.heartbeat_response = status, payload
        await until(lambda: client.closed and lease.done.done())
        assert lease.terminal
        with pytest.raises(SessionClosed, match=message) as failure:
            await call
        for key, value in details.items():
            assert failure.value.details[key] == value
        with pytest.raises(SessionClosed, match=message) as failure:
            client.node("Echo", return_call=True)
        for key, value in details.items():
            assert failure.value.details[key] == value
        assert lease_server.handshakes == lease_server.submissions == 1
    finally:
        await client.close(wait=False)
