import asyncio
import os
import threading

import pytest
from aiohttp import ClientSession, web

from comani import Client
from comani._runtime import NativeRuntime
from comani._runtime.http import create_app
from comani.errors import Expired, SessionClosed

COMFY = os.environ.get("COMANI_COMFY_ROOT", "/home/delva/projects/ComfyUI")
pytestmark = pytest.mark.skipif(not os.path.isfile(COMFY + "/execution.py"), reason="real ComfyUI checkout required")


class Echo:
    RETURN_TYPES = ("*",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("*",)}}

    def run(self, value):
        return (value,)


async def test_sdk_uses_server_session_before_first_unawaited_call_and_reclaims_closed_ids():
    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False).start()
    runtime.adapter.nodes.NODE_CLASS_MAPPINGS["SessionEcho"] = Echo
    runner = web.AppRunner(create_app(runtime))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    first_session = token = None
    try:
        for index in range(75):
            client = Client(url)
            proposed = client.session_id
            # No explicit start/await barrier before constructing either call.
            source = client.node("SessionEcho", value=index)
            dependent = client.node("SessionEcho", value=source.output())
            ref = await dependent.output()
            assert await ref.fetch() == index
            assert client.session_id != proposed
            assert client.session_id in runtime.session_seen
            assert proposed not in runtime.session_seen
            if first_session is None:
                first_session, token = client.session_id, ref.id
            await client.close()
        await runtime.queue.join()
        assert not runtime.session_seen
        assert len(runtime.closed_sessions) == 64
        assert first_session not in runtime.closed_sessions
        assert not runtime.calls and not runtime.store.entries and not runtime.requests
        for method, arguments in (
            (runtime.heartbeat, {}),
            (runtime.submit, {"call_id": "stale", "spec": {"kind": "node", "node_type": "SessionEcho", "inputs": {"value": 0}}}),
            (runtime.fetch, {"ref_id": token, "epoch": runtime.epoch}),
        ):
            with pytest.raises(SessionClosed):
                await method(session_id=first_session, **arguments)
        # Late cleanup messages are idempotent and cannot register a session.
        await runtime.close_session(first_session)
        await runtime.release(first_session, [token], runtime.epoch)
        await runtime.forget_calls(first_session, ["stale"], runtime.epoch)
        assert not runtime.session_seen
        new = await runtime.handshake(session_id=first_session)
        assert new["session_id"] != first_session
        with pytest.raises(Expired):
            await runtime.fetch(new["session_id"], token, runtime.epoch)
        await runtime.close_session(new["session_id"])
        async with ClientSession() as http:
            for stale in (first_session, "never-opened", "raw"):
                async with http.post(url + "/rpc", json={"method": "heartbeat", "params": {"session_id": stale}}) as response:
                    assert response.status == 400
                    assert (await response.json())["error"]["code"] == "SessionClosed"
        assert not runtime.requests and not runtime.session_seen
    finally:
        await runner.cleanup()


async def test_closed_inflight_session_does_not_depend_on_recent_tombstones():
    started, finish = threading.Event(), threading.Event()

    class Blocked(Echo):
        def run(self, value):
            started.set()
            assert finish.wait(10)
            return (value,)

    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False).start()
    runtime.adapter.nodes.NODE_CLASS_MAPPINGS.update(SessionEcho=Echo, SessionBlocked=Blocked)
    session = (await runtime.handshake())["session_id"]
    try:
        await runtime.submit(session, "producer", {"kind": "node", "node_type": "SessionEcho", "inputs": {"value": 42}})
        source = await runtime.wait_result(session, "producer")
        token = source["outputs"][0]["id"]
        locator = {"$ref": {"session": session, "epoch": runtime.epoch, "call_id": "producer", "slot": 0}}
        await runtime.submit(session, "blocked", {"kind": "node", "node_type": "SessionBlocked", "inputs": {"value": locator}})
        while not started.is_set():
            await asyncio.sleep(0.005)
        state = runtime.calls[(session, "blocked")]
        await runtime.close_session(session)
        assert state.inflight and session not in runtime.session_seen
        assert runtime.store.entries[token].released and runtime.store.entries[token].leases == 1
        for _ in range(70):
            other = (await runtime.handshake())["session_id"]
            await runtime.close_session(other)
        assert session not in runtime.closed_sessions
        with pytest.raises(SessionClosed):
            await runtime.heartbeat(session)
        finish.set()
        await asyncio.wait_for(runtime.queue.join(), timeout=5)
        assert not state.inflight
        assert not runtime.calls and not runtime.store.entries and not runtime.session_seen
        assert len(runtime.closed_sessions) == 64
    finally:
        finish.set()
        await runtime.close()
