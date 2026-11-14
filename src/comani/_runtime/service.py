from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import math
import sys
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial

import cloudpickle

from comani.errors import PackageConflict

from .adapter import NativeAdapter, native_python_inputs
from .objects import (
    CallFailed,
    NodeSkipped,
    ObjectStore,
    RefExpired,
    SessionClosed,
    value_from_data,
    walk_refs,
)
from .python_node import INPUT_NODE, PYTHON_NODE


@dataclass
class CallState:
    session: str
    call_id: str
    spec: dict
    done: asyncio.Future
    nodes: dict[str, asyncio.Future] = field(default_factory=dict)
    node_results: dict[str, dict] = field(default_factory=dict)
    reservations: dict[tuple[str | None, int], int] = field(default_factory=dict)
    loans: list[str] = field(default_factory=list)
    status: str = "accepted"
    task: asyncio.Task | None = None
    error: dict | None = None
    dependencies: list = field(default_factory=list)
    dependencies_released: bool = False
    captured: set[str] = field(default_factory=set)
    inflight: bool = False
    forgotten: bool = False


class NativeRuntime:
    def __init__(self, comfy_root, *, cpu=False, custom_nodes=True, session_ttl=120, comfy_args=()):
        self.epoch = uuid.uuid4().hex
        self.store = ObjectStore(self.epoch)
        self.calls: dict[tuple[str, str], CallState] = {}
        # Recent diagnostic history only. Authorization never relies on retained
        # tombstones: only server-issued sessions in session_seen are active.
        self.closed_sessions = deque(maxlen=64)
        self.queue = asyncio.Queue()
        self.events: list[dict] = []
        self.listeners: set[asyncio.Queue] = set()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="comani-native")
        self.adapter = NativeAdapter(comfy_root, cpu=cpu, custom_nodes=custom_nodes, event=self._native_event, comfy_args=comfy_args)
        self.loop = None
        self.worker = None
        self.active: CallState | None = None
        self.closing = False
        self.session_ttl = session_ttl
        self.session_seen: dict[str, float] = {}
        self.requests: dict[str, set] = {}
        self.sweeper = None
        self._starting = None

    async def start(self):
        if self._starting is None:
            self._starting = asyncio.create_task(self._start())
        await asyncio.shield(self._starting)
        return self

    async def _start(self):
        if self.worker is None:
            self.loop = asyncio.get_running_loop()
            await self.loop.run_in_executor(self.pool, self.adapter.boot)
            self.worker = asyncio.create_task(self._worker())
            self.sweeper = asyncio.create_task(self._expire_sessions())

    async def handshake(self, **_):
        await self.start()
        if self.closing:
            raise SessionClosed("This runtime is closing")
        session = uuid.uuid4().hex
        self.session_seen[session] = time.monotonic()
        return {"session_id": session, "epoch": self.epoch, "python": list(sys.version_info[:3]), "cloudpickle": cloudpickle.__version__, "nodes": self.adapter.node_types(), "session_ttl": self.session_ttl, "capabilities": {"bounded_wait": True}}

    async def artifact_put(self, session_id, artifact_id, data, kind, **_):
        self._session(session_id)
        return self.adapter.artifacts.put(artifact_id, data, kind)

    def _session(self, session):
        if self.closing or session not in self.session_seen:
            raise SessionClosed("This runtime session is unknown or closed; open a new client session")
        self.session_seen[session] = time.monotonic()

    def _open_raw_session(self):
        # The native /prompt endpoint owns one fixed history channel. It never
        # creates a per-client mapping and cannot be opened through SDK RPC.
        if self.closing:
            raise SessionClosed("This runtime is closing")
        self.session_seen.setdefault("raw", time.monotonic())

    async def heartbeat(self, session_id, **_):
        self._session(session_id)
        return {"alive": True}

    async def _expire_sessions(self):
        while True:
            await asyncio.sleep(min(10, max(0.02, self.session_ttl / 3)))
            now = time.monotonic()
            for session, seen in list(self.session_seen.items()):
                if session == "raw" or now - seen < self.session_ttl:
                    continue
                if any(not transport.is_closing() for transport in self.requests.get(session, ())):
                    self.session_seen[session] = now
                else:
                    await self.close_session(session)

    @staticmethod
    def _result(result):
        if "$error" in result:
            info = result["$error"]
            kind = {"RefExpired": RefExpired, "NodeSkipped": NodeSkipped, "SessionClosed": SessionClosed, "PackageConflict": PackageConflict}.get(info["type"], CallFailed)
            raise kind(info["message"])
        return result

    def _emit(self, kind, state=None, **data):
        event = {"type": kind, **data}
        if state is not None:
            event.update(session_id=state.session, call_id=state.call_id)
        self.events.append(event)
        if len(self.events) > 5000:
            del self.events[:1000]
        for listener in self.listeners:
            listener.put_nowait(event)

    def _native_event(self, kind, data):
        if self.loop and self.active:
            self.loop.call_soon_threadsafe(partial(self._emit, "native", self.active, native_type=kind, data=self._event_value(data)))

    @classmethod
    def _event_value(cls, value):
        # Native preview/text events include bytes and PIL images. Event history
        # must retain a wire value, never a live image/model or an unserializable
        # object that can terminate the websocket.
        if isinstance(value, (bytes, bytearray)):
            return {"codec": "bytes", "data": base64.b64encode(value).decode()}
        if isinstance(value, dict):
            return {str(key): cls._event_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._event_value(item) for item in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        from PIL import Image

        if isinstance(value, Image.Image):
            buffer = io.BytesIO()
            value.save(buffer, format="PNG")
            return {"codec": "image", "format": "PNG", "data": base64.b64encode(buffer.getvalue()).decode()}
        return repr(value)

    def _producer(self, session, locator):
        if locator.get("epoch") != self.epoch or locator.get("session", session) != session:
            raise RefExpired("Reference belongs to another runtime or session")
        state = self.calls.get((session, locator["call_id"]))
        if state is None:
            raise RefExpired("The producing call has not been submitted")
        return state

    def _port(self, state, node_id, slot):
        if node_id is None:
            if state.spec.get("kind") != "workflow":
                node_id = "result"
            else:
                selected = state.spec["graph"].get("outputs", [])
                if slot >= len(selected):
                    raise IndexError("Workflow output slot is out of range")
                ref = selected[slot]["source"]
                ref = ref.get("$graph_ref", ref)
                return (ref["node_id"], ref["slot"]) if isinstance(ref, dict) else tuple(ref)
        return node_id, slot

    def _port_ref(self, state, port):
        node_id, slot = self._port(state, *port)
        result = state.node_results.get(node_id)
        return result["outputs"][slot] if result is not None else None

    def _release_dependencies(self, state):
        if state.dependencies_released:
            return
        state.dependencies_released = True
        dependencies, state.dependencies = state.dependencies, []
        for producer, port in dependencies:
            with self.store.lock:
                producer.reservations[port] = max(0, producer.reservations.get(port, 0) - 1)
                ref = self._port_ref(producer, port)
                if ref is not None:
                    self.store.release(state.session, [ref["id"]], lease=True)
            self._collect_call(producer)

    def _collect_call(self, state):
        if not state.forgotten or state.inflight or not state.done.done() or any(state.reservations.values()):
            return
        tokens = [ref["id"] for result in state.node_results.values() for ref in result.get("outputs", [])]
        self.store.release(state.session, tokens)
        self.calls.pop((state.session, state.call_id), None)

    async def forget_calls(self, session_id, call_ids, epoch, **_):
        if epoch == self.epoch:
            for call_id in call_ids:
                state = self.calls.get((session_id, call_id))
                if state is not None:
                    state.forgotten = True
                    self._collect_call(state)
        return {"forgotten": True}

    async def submit(self, session_id, call_id, spec, **_):
        await self.start()
        self._session(session_id)
        key = (session_id, call_id)
        if key in self.calls:
            return {"call_id": call_id, "status": self.calls[key].status}
        spec = copy.deepcopy(spec)
        done = self.loop.create_future()
        state = CallState(session_id, call_id, spec, done)
        if spec.get("kind") == "workflow":
            state.nodes = {name: self.loop.create_future() for name in spec["graph"]["nodes"]}
        else:
            state.nodes = {"result": self.loop.create_future()}
        self.calls[key] = state
        try:
            unique = {json.dumps(ref, sort_keys=True): ref for ref in walk_refs(spec)}
            for locator in unique.values():
                producer = self._producer(session_id, locator)
                port = (locator.get("node_id"), locator["slot"])
                with self.store.lock:
                    producer.reservations[port] = producer.reservations.get(port, 0) + 1
                    state.dependencies.append((producer, port))
                    ref = self._port_ref(producer, port)
                    if ref is not None:
                        self.store.acquire(session_id, ref["id"])
            state.task = asyncio.create_task(self._admit(state))
        except BaseException:
            self._release_dependencies(state)
            self.calls.pop(key)
            raise
        self._emit("accepted", state)
        return {"call_id": call_id, "status": "accepted"}

    async def _locator_data(self, state, locator):
        producer = self._producer(state.session, locator)
        node_id = locator.get("node_id")
        result = self._result(await asyncio.shield(producer.nodes[node_id] if node_id is not None else producer.done))
        token = result["outputs"][locator["slot"]]["id"]
        return self.store.get(state.session, token, locator.get("epoch"), pinned=True)

    async def _admit(self, state):
        try:
            resolved = {}
            for locator in walk_refs(state.spec):
                key = json.dumps(locator, sort_keys=True)
                if key not in resolved:
                    resolved[key] = await self._locator_data(state, locator)
            self._session(state.session)
            state.status = "queued"
            await self.queue.put((state, resolved))
            self._emit("queued", state)
        except BaseException as error:
            resolved.clear()
            self._fail(state, error)

    def _capture(self, state, node_id, entry, cache, key):
        # A producer can complete in the native thread while submit reserves a
        # pending input in the HTTP loop. Lease count and publication are atomic.
        with self.store.lock:
            self._capture_locked(state, node_id, entry, cache, key)

    def _capture_locked(self, state, node_id, entry, cache, key):
        if node_id not in state.nodes or node_id in state.captured or state.nodes[node_id].done():
            return
        state.captured.add(node_id)
        node_type = state.spec.get("node_type") if state.spec.get("kind") != "workflow" else state.spec["graph"]["nodes"][node_id]["class_type"]
        definition = self.adapter.nodes.NODE_CLASS_MAPPINGS[node_type]
        list_flags = getattr(definition, "OUTPUT_IS_LIST", ())
        outputs = []
        for slot, data in enumerate(entry.outputs):
            is_list = slot < len(list_flags) and list_flags[slot]
            leases = sum(count for port, count in state.reservations.items() if self._port(state, *port) == (node_id, slot))
            if state.spec.get("kind") == "workflow":
                ref = self.store.borrow(state.session, cache, key, entry, slot, is_list=is_list, leases=leases)
            else:
                ref = self.store.own(state.session, data, leases=leases, is_list=is_list)
            outputs.append(ref)
        result = {"outputs": outputs, "names": {}}
        state.node_results[node_id] = result
        if state.session not in self.session_seen:
            self.store.release(state.session, [ref["id"] for ref in outputs])
        self.loop.call_soon_threadsafe(self._publish_node, state, node_id, result)

    def _publish_node(self, state, node_id, result):
        future = state.nodes[node_id]
        if not future.done():
            state.node_results[node_id] = result
            future.set_result(result)
            self._emit("output_ready", state, node_id=node_id, result=result)

    def _graph(self, state, resolved):
        spec = state.spec
        workflow = spec.get("kind") == "workflow"
        graph_spec = spec.get("graph", {})
        nodes = copy.deepcopy(graph_spec["nodes"]) if workflow else {"result": {"class_type": spec["node_type"], "inputs": copy.deepcopy(spec.get("inputs", {}))}}
        bindings = {}

        def source(data):
            node_id = "__bound_" + uuid.uuid4().hex
            nodes[node_id] = {"class_type": INPUT_NODE, "inputs": {"token": node_id}}
            bindings[node_id] = data
            return [node_id, 0]

        def decode(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    return source(resolved[json.dumps(value["$ref"], sort_keys=True)])
                if "$graph_ref" in value:
                    ref = value["$graph_ref"]
                    return [ref["node_id"], ref["slot"]] if isinstance(ref, dict) else list(ref)
                if "$input" in value:
                    name = value["$input"]
                    if isinstance(name, dict):
                        name = name["name"]
                    return decode(spec.get("bindings", {})[name])
                if "$value" in value:
                    descriptor = value["$value"]
                    return source([cloudpickle.loads(base64.b64decode(descriptor["data"]))])
                if any(tag in value for tag in ("$tuple", "$list", "$dict")):
                    return source([decode_container(value)])
                return value
            return value

        def decode_container(value):
            if isinstance(value, dict):
                if "$tuple" in value:
                    return tuple(decode_container(item) for item in value["$tuple"])
                if "$list" in value:
                    return [decode_container(item) for item in value["$list"]]
                if "$dict" in value:
                    return {decode_container(k): decode_container(v) for k, v in value["$dict"]}
                if "$ref" in value:
                    return value_from_data(resolved[json.dumps(value["$ref"], sort_keys=True)])
                if "$input" in value:
                    name = value["$input"]
                    return decode_container(spec.get("bindings", {})[name["name"] if isinstance(name, dict) else name])
                if "$value" in value:
                    return cloudpickle.loads(base64.b64decode(value["$value"]["data"]))
                if "$graph_ref" in value:
                    raise ValueError("A graph port inside a node container must be lifted to a Python layout port")
            return value

        original_nodes = list(nodes)
        for name in original_nodes:
            inputs = nodes[name].get("inputs", {})
            if not graph_spec.get("native", False):
                inputs = {key: decode(value) for key, value in inputs.items()}
            if nodes[name]["class_type"] == PYTHON_NODE:
                inputs = native_python_inputs(inputs)
            nodes[name]["inputs"] = inputs
        targets = graph_spec.get("targets") if workflow else ["result"]
        if not targets:
            targets = [name for name in original_nodes if getattr(self.adapter.nodes.NODE_CLASS_MAPPINGS[nodes[name]["class_type"]], "OUTPUT_NODE", False)]
        if not targets:
            raise ValueError("Workflow requires execution targets")
        return nodes, list(targets), bindings

    async def _worker(self):
        while True:
            item = await self.queue.get()
            if item is None:
                self.queue.task_done()
                return
            state, resolved = item
            bindings = {}
            graph = {}
            state.inflight = True
            try:
                self._session(state.session)
                graph, targets, bindings = self._graph(state, resolved)
                state.status = "running"
                self.active = state
                self._emit("running", state)
                def callback(node_id, entry, cache, key):
                    self._capture(state, node_id, entry, cache, key)
                result = await self.loop.run_in_executor(self.pool, lambda: self.adapter.run(graph, targets, bindings, callback, prompt_id=state.call_id, session_id=state.session, extra_data=state.spec.get("extra_data")))
                # Native callbacks are posted before the executor completion.
                await asyncio.sleep(0)
                self._finish(state, result)
            except BaseException as error:
                self._fail(state, error)
            finally:
                self.active = None
                state.inflight = False
                self._release_dependencies(state)
                resolved.clear()
                bindings.clear()
                # Native caches and hidden PROMPT/DYNPROMPT can retain this exact
                # mapping. Drop our reference without mutating their context.
                graph = {}
                self._trim_spec(state)
                if state.session not in self.session_seen:
                    self.adapter.artifacts.clear_session(state.session)
                self._collect_call(state)
                self.queue.task_done()

    @staticmethod
    def _trim_spec(state):
        if state.spec.get("kind") == "workflow":
            graph = state.spec["graph"]
            state.spec = {"kind": "workflow", "graph": {"nodes": {name: {"class_type": node["class_type"]} for name, node in graph["nodes"].items()}, "outputs": graph.get("outputs", [])}}
        else:
            state.spec = {"kind": "node", "node_type": state.spec.get("node_type")}

    def _finish(self, state, native_result):
        for name, future in state.nodes.items():
            if not future.done():
                future.set_result({"$error": {"type": "NodeSkipped", "message": f"Node {name!r} was not executed (unselected or lazy branch)"}})
        if state.spec.get("kind") == "workflow":
            outputs, names = [], {}
            for item in state.spec["graph"].get("outputs", []):
                source = item["source"]
                ref = source.get("$graph_ref", source)
                node_id, slot = (ref["node_id"], ref["slot"]) if isinstance(ref, dict) else ref
                result = self._result(state.nodes[node_id].result())
                names[item["name"]] = len(outputs)
                outputs.append(result["outputs"][slot])
            final = {"outputs": outputs, "names": names, "native": native_result}
        else:
            final = self._result(state.nodes["result"].result())
        if not state.done.done():
            state.done.set_result(final)
        state.status = "succeeded"
        self._emit("completed", state)

    def _fail(self, state, error):
        if state.done.done():
            return
        if isinstance(error, asyncio.CancelledError):
            error = SessionClosed("Call was cancelled")
        elif not isinstance(error, (CallFailed, RefExpired, NodeSkipped, SessionClosed, PackageConflict)):
            error = CallFailed(f"{type(error).__name__}: {error}")
        else:
            error = type(error)(str(error))
        info = {"type": type(error).__name__, "message": str(error)}
        state.error = info
        state.status = "cancelled" if isinstance(error, (asyncio.CancelledError, SessionClosed)) else "failed"
        for future in [state.done, *state.nodes.values()]:
            if not future.done():
                future.set_result({"$error": info})
        self._emit("failed", state, error=info)
        if not state.inflight:
            self._release_dependencies(state)
            self._trim_spec(state)
            self._collect_call(state)

    async def wait_result(self, session_id, call_id, node_id=None, wait_seconds=None, **_):
        self._session(session_id)
        if wait_seconds is not None and not (
            isinstance(wait_seconds, (int, float))
            and not isinstance(wait_seconds, bool)
            and 0 < wait_seconds <= 60
            and math.isfinite(wait_seconds)
        ):
            raise ValueError("wait_seconds must be a finite number greater than 0 and no more than 60")
        state = self.calls[(session_id, call_id)]
        future = state.done if node_id is None else state.nodes[node_id]
        if wait_seconds is None:
            result = await asyncio.shield(future)
        else:
            try:
                result = await asyncio.wait_for(asyncio.shield(future), timeout=wait_seconds)
            except TimeoutError:
                return {"pending": True}
        # Terminal node/session errors are interpreted outside the wait timeout
        # handler. A failed producer can never be mistaken for another poll.
        return self._result(result)

    async def fetch(self, session_id, ref_id, epoch, **_):
        self._session(session_id)
        value = self.store.value(session_id, ref_id, epoch)
        return {"codec": "pickle", "data": base64.b64encode(cloudpickle.dumps(value)).decode()}

    async def release(self, session_id, refs, epoch=None, **_):
        if epoch is None or epoch == self.epoch:
            self.store.release(session_id, refs)
        return {"released": True}

    async def close_session(self, session_id, **_):
        if self.session_seen.pop(session_id, None) is not None:
            self.closed_sessions.append(session_id)
        for (session, _), state in list(self.calls.items()):
            if session == session_id:
                state.forgotten = True
                if state.status not in ("succeeded", "failed", "cancelled"):
                    if state.task:
                        state.task.cancel()
                    self._fail(state, SessionClosed("Session disconnected"))
                self._collect_call(state)
        self.store.close_session(session_id)
        if self.active is None or self.active.session != session_id:
            self.adapter.artifacts.clear_session(session_id)
        self.requests.pop(session_id, None)
        return {"closed": True}

    async def close(self):
        if self.closing:
            return
        for session in set(self.session_seen) | {session for session, _ in self.calls}:
            await self.close_session(session)
        self.closing = True
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        await self.queue.put(None)
        if self.worker:
            await self.worker
        if self.loop:
            await self.loop.run_in_executor(self.pool, self.adapter.close)
        self.pool.shutdown(wait=True)
