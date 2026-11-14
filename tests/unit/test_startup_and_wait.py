import asyncio

import pytest

from comani import Client, HTTPTransport, InProcessTransport, RemoteError

from ._helpers import eventually


@pytest.mark.parametrize("entry", ["start", "context", "call"])
async def test_failed_start_closes_every_pool_and_sender(monkeypatch, entry):
    transport = HTTPTransport("http://127.0.0.1:1")
    pools = []

    async def fail(method, **params):
        pools.extend((transport._session, transport._wait_session, transport._data_session))
        raise ConnectionError("controlled connection failure")

    monkeypatch.setattr(transport, "request", fail)
    client = Client(transport=transport)
    with pytest.raises(ConnectionError, match="controlled connection failure"):
        if entry == "start":
            await client.start()
        elif entry == "context":
            async with client:
                pytest.fail("Failed startup must not enter the context")
        else:
            await client.node("Echo", value=1)
    assert len(pools) == 3 and all(pool.closed for pool in pools)
    assert client.closed and client._sender.done()
    assert client._opening.done() and client._queue.empty()


async def test_cancelled_start_closes_pools_without_aexit(monkeypatch):
    transport = HTTPTransport("http://127.0.0.1:1")
    entered = asyncio.Event()
    pools = []

    async def handshake(method, **params):
        pools.extend((transport._session, transport._wait_session, transport._data_session))
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(transport, "request", handshake)
    client = Client(transport=transport)
    task = asyncio.create_task(client.start())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert all(pool.closed for pool in pools)
    assert client.closed and client._opening.done() and client._sender.done()


async def test_bounded_wait_repolls_same_call_and_node_without_resubmission(peer):
    _, runtime = peer
    original_handshake = runtime.handshake
    original_wait = runtime.wait_result
    polls = []

    async def handshake(**params):
        result = await original_handshake(**params)
        return {**result, "capabilities": {"bounded_wait": True}}

    async def wait_result(session_id, call_id, node_id=None, wait_seconds=None):
        polls.append((session_id, call_id, node_id, wait_seconds))
        if not runtime.results[call_id].done():
            await asyncio.sleep(0.001)
            return {"pending": True}
        return await original_wait(session_id, call_id, node_id)

    runtime.handshake, runtime.wait_result = handshake, wait_result
    async with Client(transport=InProcessTransport(runtime)) as client:
        call = client.node("Block", value="done")
        cancelled_waiter = asyncio.ensure_future(call.output())
        await eventually(lambda: len(polls) >= 2)
        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter
        assert not runtime.results[call.id].cancelled()
        runtime.gate.set()
        ref, = await call
        assert await ref.fetch() == "done"
        with client.graph() as graph:
            middle, = await client.node("Echo", value=1)
            graph.output("answer", middle)
        run = client.submit(graph)
        await run[middle]
        await run
        assert len(runtime.submissions) == 2
        assert all(poll[3] == 20.0 for poll in polls)
        assert {poll[1] for poll in polls} == {call.id, run.id}
        assert any(poll[1:3] == (run.id, middle.node_id) for poll in polls)


async def test_bounded_wait_terminal_error_does_not_repoll(peer):
    _, runtime = peer
    original_handshake = runtime.handshake
    original_wait = runtime.wait_result
    polls = []

    async def handshake(**params):
        return {**await original_handshake(**params), "capabilities": {"bounded_wait": True}}

    async def wait_result(session_id, call_id, node_id=None, wait_seconds=None):
        polls.append(call_id)
        return await original_wait(session_id, call_id, node_id)

    runtime.handshake, runtime.wait_result = handshake, wait_result
    async with Client(transport=InProcessTransport(runtime)) as client:
        call = client.node("Fail")
        with pytest.raises(RemoteError, match="producer failed"):
            await call
        assert polls == [call.id]


async def test_unnegotiated_pending_is_an_error(peer):
    client, runtime = peer

    async def invalid_wait(**params):
        assert "wait_seconds" not in params
        return {"pending": True}

    runtime.wait_result = invalid_wait
    with pytest.raises(RemoteError, match="without advertising"):
        await client.node("Echo", value=1)
