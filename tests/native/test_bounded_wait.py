import asyncio
import os
import threading

import pytest

from comani._runtime import NativeRuntime
from comani.errors import Expired, RemoteError, SessionClosed, Skipped

COMFY = os.environ.get("COMANI_COMFY_ROOT", "/home/delva/projects/ComfyUI")
pytestmark = pytest.mark.skipif(not os.path.isfile(COMFY + "/execution.py"), reason="real ComfyUI checkout required")


async def test_bounded_wait_is_read_only_and_preserves_native_completion_and_errors():
    started, finish = threading.Event(), threading.Event()

    class Echo:
        RETURN_TYPES = ("INT",)
        FUNCTION = "run"

        @classmethod
        def INPUT_TYPES(cls):
            return {"required": {"value": ("INT",)}}

        def run(self, value):
            return (value,)

    class Blocked(Echo):
        async def run(self, value):
            started.set()
            while not finish.is_set():
                await asyncio.sleep(0.002)
            return (value,)

    class Fail(Echo):
        def run(self, value):
            raise TimeoutError("the node itself failed, not a wait timeout")

    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False).start()
    runtime.adapter.nodes.NODE_CLASS_MAPPINGS.update(WaitEcho=Echo, WaitBlocked=Blocked, WaitFail=Fail)
    handshake = await runtime.handshake()
    session = handshake["session_id"]
    assert handshake["capabilities"]["bounded_wait"] is True
    try:
        graph = {"kind": "workflow", "graph": {
            "native": True,
            "nodes": {
                "early": {"class_type": "WaitEcho", "inputs": {"value": 3}},
                "blocked": {"class_type": "WaitBlocked", "inputs": {"value": 42}},
                "unselected": {"class_type": "WaitFail", "inputs": {"value": 1}},
            },
            "targets": ["early", "blocked"],
            "outputs": [{"name": "value", "source": {"$graph_ref": {"node_id": "blocked", "slot": 0}}}],
        }}
        await runtime.submit(session, "once", graph)
        async with asyncio.timeout(3):
            while not started.is_set():
                await asyncio.sleep(0.005)
        state = runtime.calls[(session, "once")]
        initial_callbacks = len(state.done._callbacks or ())
        for _ in range(40):
            assert await runtime.wait_result(session, "once", wait_seconds=0.001) == {"pending": True}
        parallel = await asyncio.gather(*(runtime.wait_result(session, "once", wait_seconds=0.005) for _ in range(12)))
        assert parallel == [{"pending": True}] * 12
        for node in (None, "blocked"):
            waiter = asyncio.create_task(runtime.wait_result(session, "once", node_id=node, wait_seconds=20))
            await asyncio.sleep(0)
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await asyncio.sleep(0)
        assert not state.done.done() and not state.nodes["blocked"].done()
        assert len(state.done._callbacks or ()) == initial_callbacks
        assert not state.nodes["blocked"]._callbacks
        early = await runtime.wait_result(session, "once", node_id="early", wait_seconds=1)
        assert "outputs" in early and not state.done.done()
        for invalid in (True, False, 0, -1, 61, float("inf"), -float("inf"), float("nan"), "20", {}, 10**100):
            with pytest.raises(ValueError, match="finite number"):
                await runtime.wait_result(session, "once", wait_seconds=invalid)
        finish.set()
        result = await runtime.wait_result(session, "once", wait_seconds=2)
        assert "outputs" in result
        assert await runtime.wait_result(session, "once", wait_seconds=0.001) == result
        assert sum(event["type"] == "accepted" and event.get("call_id") == "once" for event in runtime.events) == 1
        with pytest.raises(Skipped):
            await runtime.wait_result(session, "once", node_id="unselected", wait_seconds=0.001)
        runtime.adapter.executor.caches.outputs.cache.clear()
        ref = result["outputs"][0]
        with pytest.raises(Expired):
            await runtime.fetch(session, ref["id"], ref["epoch"])
        await runtime.submit(session, "failure", {"kind": "node", "node_type": "WaitFail", "inputs": {"value": 0}})
        with pytest.raises(RemoteError, match="the node itself failed"):
            await runtime.wait_result(session, "failure", wait_seconds=2)
        finish.clear()
        started.clear()
        await runtime.submit(session, "closed", {"kind": "node", "node_type": "WaitBlocked", "inputs": {"value": 99}})
        async with asyncio.timeout(3):
            while not started.is_set():
                await asyncio.sleep(0.005)
        waiter = asyncio.create_task(runtime.wait_result(session, "closed", wait_seconds=20))
        await asyncio.sleep(0)
        await runtime.close_session(session)
        with pytest.raises(SessionClosed):
            await waiter
        finish.set()
        await runtime.queue.join()
        assert not runtime.calls and not runtime.store.entries and not runtime.session_seen
    finally:
        finish.set()
        await runtime.close()
