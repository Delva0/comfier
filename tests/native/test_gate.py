import asyncio
import base64
import os

import cloudpickle
import pytest

from comfier.daemon import NativeRuntime
from comfier.daemon.objects import NodeSkipped, RefExpired

COMFY = os.environ.get("COMFIER_COMFY_ROOT", "/home/delva/projects/ComfyUI")
pytestmark = pytest.mark.skipif(not os.path.isfile(COMFY + "/execution.py"), reason="real ComfyUI checkout required")


class Constant:
    RETURN_TYPES = ("*",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("*",)}}

    def run(self, value):
        return (value,)


class Delayed(Constant):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("*",), "delay": ("FLOAT",)}}

    async def run(self, value, delay):
        await asyncio.sleep(delay)
        return (value,)


class Fail(Constant):
    def run(self, value):
        raise RuntimeError("unselected branch ran")


class Choose(Constant):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"choose_left": ("BOOLEAN",), "left": ("*", {"lazy": True}), "right": ("*", {"lazy": True})}}

    def check_lazy_status(self, choose_left, left, right):
        return ["left"] if choose_left and left is None else (["right"] if not choose_left and right is None else [])

    def run(self, choose_left, left=None, right=None):
        return (left if choose_left else right,)


class Expand(Constant):
    def run(self, value):
        from comfy_execution.graph_utils import GraphBuilder

        graph = GraphBuilder()
        child = graph.node("GateConstant", return_call=True, value=value)
        return {"result": (child.out(0),), "expand": graph.finalize()}


class ListSource(Constant):
    OUTPUT_IS_LIST = (True,)

    def run(self, value):
        import json

        return (json.loads(value) if isinstance(value, str) else value,)


class Pair(Constant):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"left": ("*",), "right": ("*",)}}

    def run(self, left, right):
        return ((left, right),)


class Hidden(Constant):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}, "hidden": {"prompt": "PROMPT", "dynprompt": "DYNPROMPT", "unique_id": "UNIQUE_ID", "extra_pnginfo": "EXTRA_PNGINFO"}}

    def run(self, prompt, dynprompt, unique_id, extra_pnginfo):
        return ({"prompt": prompt, "node": dynprompt.get_node(unique_id), "id": unique_id, "extra": extra_pnginfo},)


class RawLink(Constant):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("*", {"rawLink": True})}, "hidden": {"dynprompt": "DYNPROMPT"}}

    def run(self, value, dynprompt):
        return ({"source": value, "class_type": dynprompt.get_node(value[0])["class_type"]},)


class Increment(Constant):
    RETURN_TYPES = ("INT",)

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("INT",)}}

    def run(self, value):
        return (value + 1,)


class Progress(Constant):
    def run(self, value):
        import comfy.utils
        from PIL import Image

        progress = comfy.utils.ProgressBar(2)
        progress.update_absolute(1)
        progress.update_absolute(2, preview=("PNG", Image.new("RGB", (2, 2), "red"), 2))
        return (value,)


def register(runtime):
    runtime.adapter.nodes.NODE_CLASS_MAPPINGS.update(GateConstant=Constant, GateDelayed=Delayed, GateFail=Fail, GateChoose=Choose, GateExpand=Expand, GateList=ListSource, GatePair=Pair, GateHidden=Hidden, GateRawLink=RawLink, GateIncrement=Increment, GateProgress=Progress)


def workflow(nodes, targets, output_node):
    return {"kind": "workflow", "graph": {"nodes": nodes, "targets": targets, "outputs": [{"name": "value", "source": {"$graph_ref": {"node_id": output_node, "slot": 0}}}], "native": True}}


async def fetched(runtime, session, result, slot=0):
    ref = result["outputs"][slot]
    encoded = await runtime.fetch(session, ref["id"], ref["epoch"])
    return cloudpickle.loads(base64.b64decode(encoded["data"]))


@pytest.mark.asyncio
async def test_native_non_output_external_object_and_queue():
    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False).start()
    session = (await runtime.handshake())["session_id"]
    try:
        runtime.adapter.nodes.NODE_CLASS_MAPPINGS.update(GateConstant=Constant, GateDelayed=Delayed)
        await runtime.submit(session, "a", {"kind": "node", "node_type": "GateDelayed", "inputs": {"value": 42, "delay": 0.15}})
        ref = {"$ref": {"session": session, "epoch": runtime.epoch, "call_id": "a", "slot": 0}}
        await runtime.submit(session, "dependent", {"kind": "node", "node_type": "GateConstant", "inputs": {"value": ref}})
        await runtime.submit(session, "independent", {"kind": "node", "node_type": "GateConstant", "inputs": {"value": "abc"}})
        await asyncio.sleep(0.02)
        assert runtime.calls[(session, "independent")].status == "queued"
        assert runtime.calls[(session, "dependent")].status == "accepted"
        assert await fetched(runtime, session, await runtime.wait_result(session, "dependent")) == 42
        assert await fetched(runtime, session, await runtime.wait_result(session, "independent")) == "abc"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_workflow_intermediate_ready_before_completion_and_expiry():
    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False).start()
    session = (await runtime.handshake())["session_id"]
    try:
        register(runtime)
        nodes = {"early": {"class_type": "GateConstant", "inputs": {"value": 42}}, "slow": {"class_type": "GateDelayed", "inputs": {"value": 7, "delay": 0.2}}}
        await runtime.submit(session, "graph", workflow(nodes, ["early", "slow"], "slow"))
        early = await runtime.wait_result(session, "graph", "early")
        assert not runtime.calls[(session, "graph")].done.done()
        assert await fetched(runtime, session, early) == 42
        await runtime.wait_result(session, "graph")
        runtime.adapter.executor.caches.outputs.cache.clear()
        with pytest.raises(RefExpired):
            await fetched(runtime, session, early)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_native_lazy_expand_and_skipped_branch():
    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False).start()
    session = (await runtime.handshake())["session_id"]
    try:
        register(runtime)
        nodes = {
            "expanded": {"class_type": "GateExpand", "inputs": {"value": 42}},
            "bad": {"class_type": "GateFail", "inputs": {"value": 1}},
            "choose": {"class_type": "GateChoose", "inputs": {"choose_left": True, "left": ["expanded", 0], "right": ["bad", 0]}},
        }
        await runtime.submit(session, "graph", workflow(nodes, ["choose"], "choose"))
        assert await fetched(runtime, session, await runtime.wait_result(session, "graph")) == 42
        with pytest.raises(NodeSkipped):
            await runtime.wait_result(session, "graph", "bad")
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_native_list_mapping_and_declared_list_output():
    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False).start()
    session = (await runtime.handshake())["session_id"]
    try:
        register(runtime)
        nodes = {"values": {"class_type": "GateList", "inputs": {"value": "[1,2,3]"}}, "mapped": {"class_type": "GateDelayed", "inputs": {"value": ["values", 0], "delay": 0.03}}}
        await runtime.submit(session, "graph", workflow(nodes, ["mapped"], "mapped"))
        assert await fetched(runtime, session, await runtime.wait_result(session, "graph")) == [1, 2, 3]
        await runtime.submit(session, "single_list", {"kind": "node", "node_type": "GateList", "inputs": {"value": {"$list": [1]}}})
        assert await fetched(runtime, session, await runtime.wait_result(session, "single_list")) == [1]
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_duplicate_dependency_lease_is_released_once():
    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False).start()
    session = (await runtime.handshake())["session_id"]
    try:
        register(runtime)
        await runtime.submit(session, "a", {"kind": "node", "node_type": "GateConstant", "inputs": {"value": 42}})
        first = await runtime.wait_result(session, "a")
        token = first["outputs"][0]["id"]
        ref = {"$ref": {"session": session, "epoch": runtime.epoch, "call_id": "a", "slot": 0}}
        await runtime.submit(session, "b", {"kind": "node", "node_type": "GatePair", "inputs": {"left": ref, "right": ref}})
        await runtime.release(session, [token])
        result = await runtime.wait_result(session, "b")
        assert await fetched(runtime, session, result) == (42, 42)
        assert token not in runtime.store.entries
        await runtime.close_session(session)
        assert not runtime.store.entries
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_abandoned_session_expires_without_manual_release():
    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False, session_ttl=0.08).start()
    session = (await runtime.handshake())["session_id"]
    try:
        register(runtime)
        await runtime.submit(session, "a", {"kind": "node", "node_type": "GateConstant", "inputs": {"value": 42}})
        await runtime.wait_result(session, "a")
        assert runtime.store.entries
        await asyncio.sleep(0.18)
        assert session in runtime.closed_sessions
        assert not runtime.store.entries
    finally:
        await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, False, None, "abc", [1, 2], (1, 2)])
async def test_python_one_slot_values(value):
    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False).start()
    session = (await runtime.handshake())["session_id"]
    try:
        import hashlib

        from comfier.daemon.python_node import PYTHON_NODE

        raw = cloudpickle.dumps(lambda: value)
        artifact = hashlib.sha256(raw).hexdigest()
        await runtime.artifact_put(session, artifact, base64.b64encode(raw).decode(), "pickle")
        await runtime.submit(session, "a", {"kind": "node", "node_type": PYTHON_NODE, "inputs": {"code": {"kind": "pickle", "artifact_id": artifact}, "layout": {"args": {"$tuple": []}, "kwargs": {"$dict": []}}}})
        actual = await fetched(runtime, session, await runtime.wait_result(session, "a"))
        assert actual == value
        assert type(actual) is type(value)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_native_hidden_prompt_alias_and_raw_link_source_graph():
    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False).start()
    session = (await runtime.handshake())["session_id"]
    try:
        register(runtime)
        await runtime.submit(session, "hidden", {"kind": "node", "node_type": "GateHidden", "extra_data": {"extra_pnginfo": {"test": 42}}})
        result = await runtime.wait_result(session, "hidden")
        await asyncio.sleep(0)
        value = await fetched(runtime, session, result)
        assert value["id"] == "result"
        assert value["prompt"]["result"]["class_type"] == "GateHidden"
        assert value["node"]["class_type"] == "GateHidden"
        assert value["extra"] == {"test": 42}
        nodes = {"source": {"class_type": "GateConstant", "inputs": {"value": 1}}, "raw": {"class_type": "GateRawLink", "inputs": {"value": ["source", 0]}}}
        await runtime.submit(session, "raw", workflow(nodes, ["raw"], "raw"))
        assert await fetched(runtime, session, await runtime.wait_result(session, "raw")) == {"source": ["source", 0], "class_type": "GateConstant"}
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_official_v3_dynamic_combo_loop_with_carried_value():
    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False).start()
    session = (await runtime.handshake())["session_id"]
    try:
        register(runtime)
        nodes = {
            "initial": {"class_type": "GateIncrement", "inputs": {"value": -1}},
            "loop": {"class_type": "StartLoop", "inputs": {"mode": "simple", "mode.num_iterations": 3, "cache_iterations": False, "initial_iteration_value": ["initial", 0]}},
            "increment": {"class_type": "GateIncrement", "inputs": {"value": ["loop", 4]}},
            "close": {"class_type": "EndLoop", "inputs": {"output_value": ["increment", 0], "next_iteration_value": ["increment", 0], "accumulate": False}},
        }
        await runtime.submit(session, "loop", workflow(nodes, ["close"], "close"))
        assert await fetched(runtime, session, await runtime.wait_result(session, "loop")) == [3]
        assert "_loop_body" not in nodes["loop"]
        nodes["initial"]["inputs"]["value"] = 9
        await runtime.submit(session, "loop-again", workflow(nodes, ["close"], "close"))
        assert await fetched(runtime, session, await runtime.wait_result(session, "loop-again")) == [13]
        assert "_loop_body" not in nodes["loop"]
        assert any(event.get("native_type") == 3 for event in runtime.events)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_forgotten_pending_call_executes_and_records_are_collected():
    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False).start()
    session = (await runtime.handshake())["session_id"]
    try:
        register(runtime)
        await runtime.submit(session, "producer", {"kind": "node", "node_type": "GateDelayed", "inputs": {"value": 42, "delay": 0.05}})
        ref = {"$ref": {"session": session, "epoch": runtime.epoch, "call_id": "producer", "slot": 0}}
        await runtime.submit(session, "consumer", {"kind": "node", "node_type": "GateConstant", "inputs": {"value": ref}})
        await runtime.forget_calls(session, ["producer"], runtime.epoch)
        assert (session, "producer") in runtime.calls
        assert await fetched(runtime, session, await runtime.wait_result(session, "consumer")) == 42
        await runtime.forget_calls(session, ["consumer"], runtime.epoch)
        await runtime.queue.join()
        assert not runtime.calls
        assert not runtime.store.entries
        await runtime.submit(session, "borrowed", workflow({"a": {"class_type": "GateConstant", "inputs": {"value": 7}}}, ["a"], "a"))
        await runtime.wait_result(session, "borrowed")
        await runtime.forget_calls(session, ["borrowed"], runtime.epoch)
        await runtime.queue.join()
        assert not runtime.calls
        assert not runtime.store.entries
        assert runtime.adapter.executor.caches.outputs.cache
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_borrowed_ref_does_not_revive_after_same_key_is_recomputed():
    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False).start()
    session = (await runtime.handshake())["session_id"]
    try:
        register(runtime)
        spec = workflow({"a": {"class_type": "GateConstant", "inputs": {"value": 7}}}, ["a"], "a")
        await runtime.submit(session, "first", spec)
        first = await runtime.wait_result(session, "first")
        assert await fetched(runtime, session, first) == 7
        runtime.adapter.executor.caches.outputs.cache.clear()
        await runtime.submit(session, "second", spec)
        assert await fetched(runtime, session, await runtime.wait_result(session, "second")) == 7
        with pytest.raises(RefExpired):
            await fetched(runtime, session, first)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_accepted_borrowed_dependency_has_temporary_input_pin_only():
    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False).start()
    session = (await runtime.handshake())["session_id"]
    try:
        register(runtime)
        spec = workflow({"a": {"class_type": "GateConstant", "inputs": {"value": 7}}}, ["a"], "a")
        await runtime.submit(session, "producer", spec)
        first = await runtime.wait_result(session, "producer")
        ref = {"$ref": {"session": session, "epoch": runtime.epoch, "call_id": "producer", "slot": 0}}
        await runtime.submit(session, "consumer", {"kind": "node", "node_type": "GateConstant", "inputs": {"value": ref}})
        runtime.adapter.executor.caches.outputs.cache.clear()
        with pytest.raises(RefExpired):
            await fetched(runtime, session, first)
        assert await fetched(runtime, session, await runtime.wait_result(session, "consumer")) == 7
        await runtime.queue.join()
        borrowed = runtime.store.entries[first["outputs"][0]["id"]]
        assert borrowed.leases == 0 and borrowed.data is None
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_native_progress_bar_and_preview_are_forwarded_as_wire_values():
    import json

    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False).start()
    session = (await runtime.handshake())["session_id"]
    previous_hook = runtime.adapter.previous_progress_hook
    try:
        register(runtime)
        await runtime.submit(session, "progress", {"kind": "node", "node_type": "GateProgress", "inputs": {"value": 42}})
        assert await fetched(runtime, session, await runtime.wait_result(session, "progress")) == 42
        progress = [event for event in runtime.events if event.get("native_type") == "progress"]
        assert progress[-1]["data"] == {"value": 2, "max": 2, "prompt_id": "progress", "node": "result"}
        preview = next(event for event in runtime.events if event.get("native_type") == 2)
        assert preview["data"][1]["codec"] == "image"
        assert base64.b64decode(preview["data"][1]["data"]).startswith(b"\x89PNG")
        json.dumps(runtime.events)
    finally:
        await runtime.close()
    import comfy.utils

    assert comfy.utils.PROGRESS_BAR_HOOK is previous_hook
