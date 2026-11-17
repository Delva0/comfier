import asyncio
import base64
import sys

import cloudpickle
import pytest

from comfier import Client, InProcessTransport
from comfier.protocol import decode_tree, pack_pickle, restore_arguments


class FakeRuntime:
    def __init__(self):
        self.epoch = "test-epoch"
        self.submissions, self.released, self.effects = [], [], []
        self.forgotten = []
        self.artifacts, self.objects, self.results = {}, {}, {}
        self.gate = asyncio.Event()

    async def handshake(self, session_id):
        return {"epoch": self.epoch, "python": list(sys.version_info[:3]),
                "capabilities": {"python_inputs": 1},
                "cloudpickle": cloudpickle.__version__, "nodes": {
                    name: {"outputs": ["*"] * count} for name, count in
                    [("Echo", 1), ("Block", 1), ("Pair", 2), ("Effect", 0),
                     ("Fail", 1), ("ComaniPython", 1), ("原始 节点-name", 1)]}}

    async def submit(self, session_id, call_id, spec):
        self.submissions.append((call_id, spec))
        done = self.results[call_id] = asyncio.get_running_loop().create_future()
        done.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        asyncio.create_task(self.execute(spec, done))
        return {"accepted": True}

    async def resolve(self, value):
        if isinstance(value, dict) and "$ref" in value:
            ref = value["$ref"]
            result = await self.results[ref["call_id"]]
            return self.objects[result["outputs"][ref["slot"]]["id"]]
        if isinstance(value, dict):
            if set(value) == {"$dict"}:
                return {await self.resolve(k): await self.resolve(v) for k, v in value["$dict"]}
            if set(value) == {"$list"}:
                return [await self.resolve(v) for v in value["$list"]]
            if set(value) == {"$tuple"}:
                return tuple([await self.resolve(v) for v in value["$tuple"]])
            if set(value) == {"$value"}:
                return decode_tree(value)
            return {k: await self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [await self.resolve(v) for v in value]
        return value

    def info(self, value, owner="owned"):
        key = f"object-{len(self.objects)}"
        self.objects[key] = value
        return {"id": key, "epoch": self.epoch, "owner": owner}

    async def execute(self, spec, done):
        try:
            if spec["kind"] == "workflow":
                done.set_result({"outputs": [self.info(v["name"], "borrowed")
                                            for v in spec["graph"]["outputs"]]})
                return
            inputs = {k: await self.resolve(v) for k, v in spec["inputs"].items()}
            name = spec["node_type"]
            if name == "Block":
                await self.gate.wait()
            if name == "Fail":
                raise ValueError("producer failed")
            if name == "Effect":
                self.effects.append(inputs)
                values = []
            elif name == "Pair":
                values = [inputs["value"], inputs["value"]]
            elif name == "ComaniPython":
                function = cloudpickle.loads(self.artifacts[inputs["code"]["artifact_id"]])
                args, kwargs = restore_arguments(inputs["layout"], inputs)
                values = [function(*args, **kwargs)]
            else:
                values = [inputs.get("value", inputs)]
            done.set_result({"outputs": [self.info(v) for v in values]})
        except Exception as error:
            done.set_exception(error)

    async def wait_result(self, session_id, call_id, node_id=None):
        if node_id is not None:
            await self.results[call_id]
            return {"outputs": [self.info(f"middle:{node_id}", "borrowed")]}
        return await self.results[call_id]

    async def fetch(self, session_id, ref_id, epoch):
        return pack_pickle(self.objects[ref_id])

    async def release(self, session_id, refs, epoch):
        self.released.extend(refs)
        return {}

    async def artifact_put(self, session_id, artifact_id, kind, data):
        self.artifacts[artifact_id] = base64.b64decode(data)
        return {"artifact_id": artifact_id}

    async def close_session(self, session_id):
        return {}

    async def forget_calls(self, session_id, call_ids, epoch):
        self.forgotten.extend(call_ids)
        return {"forgotten": len(call_ids)}


@pytest.fixture
async def peer():
    runtime = FakeRuntime()
    async with Client(transport=InProcessTransport(runtime)) as client:
        yield client, runtime
