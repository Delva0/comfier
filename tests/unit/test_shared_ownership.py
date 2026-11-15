"""Public handles share the HTTP session; internal waiters do not own it."""

import asyncio
import contextvars
import gc
import threading
import time
import weakref

import pytest
from aiohttp import web

from comani import Client, HTTPTransport, Released, RemoteError, SessionClosed
from comani.graph import _receiver

from .test_heartbeat import LeaseServer, until


class OwnershipServer(LeaseServer):
    output_count = 1
    fail_results = False

    async def rpc(self, request):
        body = await request.json()
        if body["method"] == "wait_result":
            if self.fail_results:
                return web.json_response({"result": {"error": {
                    "code": "RemoteError", "message": "controlled producer failure",
                }}})
            if self.output_count == 0:
                return web.json_response({"result": {"outputs": []}})
        return await super().rpc(request)


@pytest.fixture
def owner_server():
    server = OwnershipServer()
    yield server
    server.close()


async def returned_handle(server, kind):
    client = await Client(server.url).start()
    call = client.node("Echo")
    handle = call if kind == "call" else call.output()
    if kind == "ref":
        handle = await handle
    return handle, weakref.ref(client), client._lease, client.transport


@pytest.mark.parametrize("kind", ["call", "future", "ref"])
async def test_helper_returned_handle_keeps_session_until_last_owner_disappears(owner_server, kind):
    owner_server.block_results = kind != "ref"
    handle, client_ref, lease, transport = await returned_handle(owner_server, kind)
    gc.collect()
    assert client_ref() is not None
    before = owner_server.heartbeats
    await until(lambda: owner_server.heartbeats > before)
    assert owner_server.closes == 0
    owner_server.loop.call_soon_threadsafe(owner_server.gate.set)
    if kind == "call":
        ref, = await handle
    else:
        ref = handle if kind == "ref" else await handle
    assert await ref.fetch() == 12345
    del handle, ref
    gc.collect()
    await until(lambda: client_ref() is None and lease.done.done() and transport._session is None)
    assert owner_server.handshakes == owner_server.submissions == owner_server.closes == 1
    assert not lease._thread.is_alive()


async def test_one_output_alias_keeps_session_after_other_aliases_disappear(owner_server):
    future, client_ref, lease, transport = await returned_handle(owner_server, "future")
    ref = await future
    last_alias = await future
    del future, ref
    gc.collect()
    assert client_ref() is not None and await last_alias.fetch() == 12345
    del last_alias
    gc.collect()
    await until(lambda: client_ref() is None and lease.done.done() and transport._session is None)
    assert owner_server.closes == 1


async def test_release_revokes_all_output_aliases_and_their_session_ownership(owner_server):
    future, client_ref, lease, transport = await returned_handle(owner_server, "future")
    ref = await future
    ref.release()
    future.release()
    with pytest.raises(Released):
        await future
    with pytest.raises(Released):
        await ref.fetch()
    gc.collect()
    await until(lambda: client_ref() is None and lease.done.done() and transport._session is None)
    assert owner_server.closes == 1


@pytest.mark.parametrize("workflow", [False, True])
async def test_retained_call_is_an_independent_owner_after_all_outputs_are_released(
        owner_server, workflow):
    async def make_call():
        client = await Client(owner_server.url).start()
        if workflow:
            with client.graph() as graph:
                captured = client.node("Echo")
                graph.output("value", captured.output())
            call = client.submit(graph)
        else:
            call = client.node("Echo")
        return call, weakref.ref(client), client._lease, client.transport

    call, client_ref, lease, transport = await make_call()
    future = call.output()
    ref = await future
    ref.release()
    gc.collect()
    assert client_ref() is not None and not lease.done.done()
    assert owner_server.closes == 0
    with pytest.raises(Released):
        await future
    del call
    gc.collect()
    await until(lambda: client_ref() is None and lease.done.done() and transport._session is None)
    assert owner_server.closes == 1


async def test_zero_output_call_keeps_its_session_without_an_owner_cell(owner_server):
    owner_server.output_count = 0
    call, client_ref, lease, transport = await returned_handle(owner_server, "call")
    assert await call == ()
    gc.collect()
    assert call._outputs == {} and client_ref() is not None
    del call
    gc.collect()
    await until(lambda: client_ref() is None and lease.done.done() and transport._session is None)


async def test_explicitly_retained_graph_remains_a_public_session_owner(owner_server):
    async def graph_and_ref():
        client = await Client(owner_server.url).start()
        with client.graph() as graph:
            graph.output("value", client.node("Echo").output())
        ref = await client.submit(graph).output()
        return graph, ref, weakref.ref(client), client._lease, client.transport

    graph, ref, client_ref, lease, transport = await graph_and_ref()
    ref.release()
    gc.collect()
    assert graph.client is client_ref() and not lease.done.done()
    del graph
    gc.collect()
    await until(lambda: client_ref() is None and lease.done.done() and transport._session is None)


async def test_explicit_close_revokes_live_pending_call_and_output_owners(owner_server):
    owner_server.block_results = True
    call, client_ref, lease, transport = await returned_handle(owner_server, "call")
    future = call.output()
    await client_ref().close(wait=False)
    assert client_ref().closed and lease.done.done() and transport._session is None
    with pytest.raises(SessionClosed):
        await call
    with pytest.raises(SessionClosed):
        await future


async def test_context_exit_revokes_a_returned_ready_ref(owner_server):
    async def scoped_ref():
        async with Client(owner_server.url) as client:
            return await client.node("Echo").output(), client._lease

    ref, lease = await scoped_ref()
    assert lease.done.done() and not lease._thread.is_alive()
    with pytest.raises(SessionClosed):
        await ref.fetch()


async def test_unowned_hung_watcher_does_not_root_client_after_ack(owner_server):
    owner_server.block_results = True
    call, client_ref, lease, transport = await returned_handle(owner_server, "call")
    await asyncio.shield(call._state.accepted)
    assert owner_server.submissions == 1 and call.status == "accepted"
    state_ref = weakref.ref(call._state)
    del call
    gc.collect()
    await until(lambda: client_ref() is None and lease.done.done() and transport._session is None)
    gc.collect()
    assert state_ref() is None and owner_server.closes == 1


async def test_failed_public_call_traceback_does_not_create_a_hidden_client_owner(owner_server):
    owner_server.fail_results = True

    async def fail_and_drop():
        client = await Client(owner_server.url).start()
        call = client.node("Echo")
        state_ref = weakref.ref(call._state)
        try:
            await call
        except RemoteError:
            pass
        return weakref.ref(client), state_ref, client._lease, client.transport

    client_ref, state_ref, lease, transport = await fail_and_drop()
    await asyncio.sleep(0)
    gc.collect()
    await until(lambda: client_ref() is None and lease.done.done() and transport._session is None)
    gc.collect()
    assert state_ref() is None


@pytest.mark.parametrize("control", ["release", "forget"])
@pytest.mark.parametrize("capture", [False, True])
async def test_queued_gc_control_callback_does_not_own_client_during_loop_block(
        owner_server, control, capture):
    if control == "release":
        ref, client_ref, lease, transport = await returned_handle(owner_server, "ref")
        if capture:
            with client_ref().graph():
                ref.release()
        else:
            ref.release()
    else:
        owner_server.output_count = 0
        call, client_ref, lease, transport = await returned_handle(owner_server, "call")
        assert await call == ()
        if capture:
            with client_ref().graph():
                del call
        else:
            del call
    try:
        gc.collect()
        # No business-loop callback can run, but the keeper can shut down on its
        # own loop once the last public session owner has disappeared.
        time.sleep(owner_server.ttl * 2)
        assert client_ref() is None and lease.done.done()
        await until(lambda: transport._session is None)
        assert not lease._thread.is_alive() and owner_server.closes == 1
    finally:
        remaining = client_ref()
        if remaining is not None:
            await remaining.close(wait=False)


@pytest.mark.parametrize("capture", [False, True])
async def test_internal_start_isolates_context_without_changing_user_context(
        owner_server, monkeypatch, capture):
    marker = contextvars.ContextVar("application_marker", default=None)
    observed = []
    transport = HTTPTransport(owner_server.url)
    request = transport.request

    async def observe_request(method, **params):
        observed.append((method, _receiver.get() is not None, marker.get() is not None))
        return await request(method, **params)

    monkeypatch.setattr(transport, "request", observe_request)
    client = Client(transport=transport)
    token = marker.set(client)
    graph = None
    try:
        if capture:
            with client.graph() as graph:
                await client.start()
                assert _receiver.get() is graph and marker.get() is client
        else:
            await client.start()
            assert marker.get() is client
    finally:
        marker.reset(token)
    client_ref, lease = weakref.ref(client), client._lease
    del graph, client
    try:
        assert observed[0] == ("handshake", False, False)
        gc.collect()
        await until(lambda: client_ref() is None and lease.done.done() and transport._session is None)
        assert owner_server.closes == 1
    finally:
        remaining = client_ref()
        if remaining is not None:
            await remaining.close(wait=False)


async def test_pending_state_internal_callbacks_do_not_keep_application_client_context(owner_server):
    owner_server.block_results = True
    marker = contextvars.ContextVar("state_application_marker", default=None)
    client = await Client(owner_server.url).start()
    token = marker.set(client)
    try:
        call = client.node("Echo")
        assert marker.get() is client
    finally:
        marker.reset(token)
    await asyncio.shield(call._state.accepted)
    client_ref, lease, transport = weakref.ref(client), client._lease, client.transport
    del call, client
    try:
        gc.collect()
        await until(lambda: client_ref() is None and lease.done.done() and transport._session is None)
    finally:
        remaining = client_ref()
        if remaining is not None:
            await remaining.close(wait=False)


async def test_keeper_thread_construction_and_start_use_fresh_context(owner_server, monkeypatch):
    marker = contextvars.ContextVar("thread_application_marker", default=None)
    original_thread = threading.Thread
    observed = []

    class ObservedThread(original_thread):
        def __init__(self, *args, **kwargs):
            observed.append(("construct", marker.get()))
            super().__init__(*args, **kwargs)

        def start(self):
            observed.append(("start", marker.get()))
            return super().start()

    transport = HTTPTransport(owner_server.url)
    await transport.open()
    session = (await transport.request("handshake"))["session_id"]
    monkeypatch.setattr("comani.transport.threading.Thread", ObservedThread)
    token = marker.set("caller-value")
    try:
        lease = transport._start_heartbeat(session, 0.1, asyncio.get_running_loop(), lambda *args: None)
        assert marker.get() == "caller-value"
    finally:
        marker.reset(token)
    try:
        await asyncio.wrap_future(lease.ready)
        assert observed == [("construct", None), ("start", None)]
    finally:
        await lease.close()
        await transport.request("close_session", session_id=session)
        await transport.close()
