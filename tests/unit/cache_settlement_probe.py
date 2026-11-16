"""CPU-only subprocess probe: a stalled native worker cannot stall pytest cleanup."""

import asyncio
import json
import os
import threading
import time


async def probe():
    from comani import Client, InProcessTransport
    from comani.daemon import NativeRuntime

    runtime = await NativeRuntime(
        os.environ.get("COMANI_COMFY_ROOT", "/home/delva/projects/ComfyUI"),
        cpu=True, custom_nodes=False,
    ).start()
    started, finish, ended = threading.Event(), threading.Event(), threading.Event()

    class Constant:
        RETURN_TYPES = ("INT",)
        FUNCTION = "run"

        @classmethod
        def INPUT_TYPES(cls):
            return {"required": {"value": ("INT",)}}

        def run(self, value):
            return (value,)

    class Blocked(Constant):
        async def run(self, value):
            started.set()
            while not finish.is_set():
                await asyncio.sleep(0.01)
            ended.set()
            return (value,)

    runtime.adapter.nodes.NODE_CLASS_MAPPINGS.update(ProbeConstant=Constant, ProbeBlocked=Blocked)
    client = await Client(transport=InProcessTransport(runtime)).start()
    original_set = runtime.adapter.executor.caches.outputs.set
    original_capture = runtime.adapter._capture
    report = {}
    try:
        async def evict_after_set(node_id, entry):
            await original_set(node_id, entry)
            if node_id == "target":
                owner, key = runtime.adapter._cache_location(node_id)
                owner.cache.pop(key, None)

        runtime.adapter.executor.caches.outputs.set = evict_after_set
        raw = {"target": {"class_type": "ProbeConstant", "inputs": {"value": 7}}}
        call = client.submit(client.graph(raw, outputs={"value": ["target", 0]}, targets=["target"]), return_call=True)
        try:
            result = await asyncio.wait_for(call.output("value"), timeout=1)
            try:
                await result.fetch()
                outcome = "ready"
            except Exception as error:
                outcome = type(error).__name__
        except TimeoutError:
            state = runtime.calls[(client.session_id, call.id)]
            report["eviction"] = {"outcome": "pending", "status": state.status,
                "node_done": state.nodes["target"].done(), "whole_done": state.done.done()}
            print(json.dumps(report), flush=True)
            os._exit(3)
        except Exception as error:
            outcome = type(error).__name__
        report["eviction"] = {"outcome": outcome}
        runtime.adapter.executor.caches.outputs.set = original_set

        # Simulate losing the adapter's result observation entirely. A native
        # terminal result still must settle the requested output as not produced.
        runtime.adapter._capture = lambda node_id, entry: None
        missed = client.submit(client.graph(raw, outputs={"value": ["target", 0]}, targets=["target"]), return_call=True)
        try:
            await asyncio.wait_for(missed.output("value"), timeout=1)
            outcome = "ready"
        except TimeoutError:
            report["missed_capture"] = {"outcome": "pending"}
            print(json.dumps(report), flush=True)
            os._exit(3)
        except Exception as error:
            outcome = type(error).__name__
        report["missed_capture"] = {"outcome": outcome}
        runtime.adapter._capture = original_capture

        active = client.node("ProbeBlocked", return_call=True, value=9)
        deadline = time.monotonic() + 1
        while not started.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert started.is_set(), "Controlled CPU worker never started"
        state = runtime.calls[(client.session_id, active.id)]
        await client.close(wait=False)
        report["closed_session"] = {
            "queue_size": runtime.queue.qsize(), "active": runtime.active is state,
            "inflight": state.inflight, "native_ended": ended.is_set(),
            "whole_done": state.done.done(), "status": state.status,
        }
        finish.set()
        deadline = time.monotonic() + 2
        while state.inflight and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        report["closed_session"]["eventually_finished"] = ended.is_set() and not state.inflight
        print(json.dumps(report), flush=True)
    finally:
        finish.set()
        runtime.adapter.executor.caches.outputs.set = original_set
        runtime.adapter._capture = original_capture
        await client.close(wait=False)
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(probe())
