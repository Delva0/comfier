from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import heapq
import hmac
import io
import json
import math
import secrets
import sys
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial

import cloudpickle

from comani._codec import has_lz4
from comani.errors import PackageConflict
from comani.files import FILE_CHUNK_SIZE, FILE_VERSION, _io
from comani.protocol import BINARY_FETCH_LZ4_VERSION, BINARY_FETCH_VERSION

from . import file_http
from .adapter import NativeAdapter, native_python_inputs
from .files import FileStore
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
    reservations: dict[tuple[str | None, int | None], int] = field(default_factory=dict)
    loans: list[str] = field(default_factory=list)
    status: str = "accepted"
    task: asyncio.Task | None = None
    error: dict | None = None
    dependencies: list = field(default_factory=list)
    dependencies_released: bool = False
    captured: set[str] = field(default_factory=set)
    inflight: bool = False
    forgotten: bool = False
    digest: str = ""


@dataclass(frozen=True)
class RecoveryPolicy:
    """Deployment limits; the deadline advances only on authenticated traffic."""

    offline_grace: float = 3600
    max_sessions: int = 256
    max_call_records: int = 1000000
    max_objects: int = 100000
    submission_seconds: float = 3600
    expiry_running: str = "finish_and_discard"

    def __post_init__(self):
        if not math.isfinite(self.offline_grace) or self.offline_grace <= 0:
            raise ValueError("offline_grace must be a positive finite duration")
        if not math.isfinite(self.submission_seconds) or self.submission_seconds <= 0:
            raise ValueError("submission_seconds must be positive and finite")
        if any(type(value) is not int or value < 1 for value in (self.max_sessions, self.max_call_records, self.max_objects)):
            raise ValueError("Recovery capacities must be positive")
        if self.expiry_running != "finish_and_discard":
            raise ValueError("Supported expiry_running policy: finish_and_discard")


class RecoveryError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class NativeRuntime:
    def __init__(self, comfy_root, *, cpu=False, custom_nodes=True, session_ttl=None, comfy_args=(),
                 max_file_bytes=None, recovery_policy=None):
        if max_file_bytes is not None and (type(max_file_bytes) is not int or max_file_bytes < 0):
            raise ValueError("max_file_bytes must be None or a nonnegative integer")
        self.max_file_bytes = max_file_bytes
        self.recovery_policy = recovery_policy or RecoveryPolicy(
            offline_grace=3600 if session_ttl is None else session_ttl)
        self._files = None
        self.epoch = uuid.uuid4().hex
        self.store = ObjectStore(self.epoch, max_entries=self.recovery_policy.max_objects)
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
        self.session_ttl = self.recovery_policy.offline_grace
        self.session_tokens: dict[str, str] = {}
        # Retain hashes independently of CallState: forget must not make a
        # possibly accepted side effect eligible for a second execution.
        self.submissions: dict[tuple[str, str], tuple[str, float | None]] = {}
        self._submission_secret = secrets.token_bytes(32)
        self._submission_deadlines = []
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
        if len(self.session_seen) >= self.recovery_policy.max_sessions:
            raise RecoveryError("RecoveryCapacity", "The runtime session capacity is full")
        session = uuid.uuid4().hex
        self.session_seen[session] = time.monotonic()
        self.session_tokens[session] = secrets.token_urlsafe(32)
        return self._handshake(session)

    def _handshake(self, session):
        capabilities = {"bounded_wait": True, "binary_fetch": BINARY_FETCH_VERSION,
                        "files": FILE_VERSION, "session_resume": 1, "whole_call_refs": 1}
        if has_lz4():
            capabilities["binary_fetch_lz4"] = BINARY_FETCH_LZ4_VERSION
        return {"session_id": session, "resume_token": self.session_tokens.get(session), "submission_window": self._submission_window(session), "epoch": self.epoch, "python": list(sys.version_info[:3]), "cloudpickle": cloudpickle.__version__, "nodes": self.adapter.node_types(), "session_ttl": self.session_ttl, "recovery_limits": {"max_sessions": self.recovery_policy.max_sessions, "max_objects": self.recovery_policy.max_objects, "max_call_records": self.recovery_policy.max_call_records, "submission_seconds": self.recovery_policy.submission_seconds, "expiry_running": self.recovery_policy.expiry_running}, "capabilities": capabilities, "file_limits": {"max_file_bytes": self.max_file_bytes, "chunk_bytes": FILE_CHUNK_SIZE}}

    async def resume(self, session_id, resume_token, epoch, **_):
        if epoch != self.epoch:
            raise RefExpired("The runtime restarted; this session has expired")
        expected = self.session_tokens.get(session_id)
        if (not isinstance(resume_token, str) or expected is None
                or not hmac.compare_digest(expected, resume_token)):
            raise SessionClosed("Invalid or expired session recovery identity")
        self._session(session_id)
        return self._handshake(session_id)

    def _submission_window(self, session):
        expires_at = time.time() + self.recovery_policy.submission_seconds
        deadline = str(expires_at)
        signature = hmac.new(self._submission_secret, f"{session}:{deadline}".encode(), hashlib.sha256).hexdigest()
        return {"token": deadline + "." + signature, "expires_at": expires_at}

    async def submission_window(self, session_id, epoch, **_):
        if epoch != self.epoch:
            raise RefExpired("The runtime restarted; this submission window expired")
        self._session(session_id)
        return self._submission_window(session_id)

    def _validate_window(self, session, window):
        # Legacy in-process/direct clients have no signed window. Their hashes
        # remain until explicit session close, subject to the same hard quota.
        if window is None:
            return None
        try:
            deadline, signature = window["token"].rsplit(".", 1)
            expires_at = float(deadline)
            expected = hmac.new(self._submission_secret, f"{session}:{deadline}".encode(), hashlib.sha256).hexdigest()
            if not math.isfinite(expires_at) or not hmac.compare_digest(signature, expected):
                raise ValueError
        except (TypeError, ValueError, KeyError, AttributeError):
            raise RecoveryError("CallExpired", "Invalid submission window") from None
        return expires_at

    def _prune_submissions(self):
        now = time.time()
        while self._submission_deadlines and self._submission_deadlines[0][0] <= now:
            deadline, key = heapq.heappop(self._submission_deadlines)
            current = self.submissions.get(key)
            if current is not None and current[1] == deadline:
                self.submissions.pop(key, None)

    async def runtime_info(self, include_node_metadata=False, **_):
        await self.start()
        import folder_paths

        directories = {name: getattr(folder_paths, f"get_{name}_directory")()
                       for name in ("input", "output", "temp")}
        categories = {name: list(folder_paths.get_folder_paths(name))
                      for name in folder_paths.folder_names_and_paths
                      if name not in {"custom_nodes", "datasets"}}
        return {"epoch": self.epoch, "active": self.active is not None,
                "pending": sum(state.status in {"accepted", "queued", "running"}
                               for state in self.calls.values()),
                "sessions": len(self.session_seen),
                **({"node_metadata": {"epoch": self.epoch, "nodes": self.adapter.node_types()}}
                   if include_node_metadata else {}),
                "roots": {"directories": directories, "categories": categories,
                          "custom_nodes": list(folder_paths.get_folder_paths("custom_nodes")),
                          "aliases": {name: folder_paths.map_legacy(name) for name in ("clip", "unet")}}}

    async def file_invalidate(self, category=None, **_):
        import folder_paths

        if category is not None:
            folder_paths.filename_list_cache.pop(category, None)
        return {"invalidated": True}

    def _file_store(self):
        if self._files is None:
            import folder_paths

            self._files = FileStore(folder_paths, max_file_size=self.max_file_bytes,
                                    chunk_size=FILE_CHUNK_SIZE)
        return self._files

    async def file_roots(self, **params):
        return await file_http.file_roots(self, **params)

    async def file_stat(self, **params):
        return await file_http.file_stat(self, **params)

    async def file_remove(self, **params):
        return await file_http.file_remove(self, **params)

    async def artifact_put(self, session_id, artifact_id, data, kind, **_):
        self._session(session_id)
        return self.adapter.artifacts.put(artifact_id, data, kind)

    def _session(self, session, *, touch=True):
        if self.closing or session not in self.session_seen:
            raise SessionClosed("This runtime session is unknown or closed; open a new client session")
        if session != "raw" and time.monotonic() - self.session_seen[session] >= self.session_ttl:
            asyncio.create_task(self.close_session(session))
            raise SessionClosed("The disconnected session recovery grace has expired")
        if touch:
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
            self._prune_submissions()
            now = time.monotonic()
            for session, seen in list(self.session_seen.items()):
                if session == "raw" or now - seen < self.session_ttl:
                    continue
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
        try:
            node_id, slot = self._port(state, *port)
        except (IndexError, KeyError):
            return None
        result = state.node_results.get(node_id)
        return result["outputs"][slot] if result is not None and slot < len(result["outputs"]) else None

    def _dependency_refs(self, state, port):
        if port[1] is None:
            result = state.node_results.get(port[0] or "result")
            return result["outputs"] if result is not None else ()
        ref = self._port_ref(state, port)
        return (ref,) if ref is not None else ()

    @staticmethod
    def _locator_port(locator):
        if locator.get("whole") is True:
            if "slot" in locator or locator.get("node_id") is not None:
                raise ValueError("A whole-call reference cannot select an output slot or node")
            return None, None
        return locator.get("node_id"), locator["slot"]

    def _release_dependencies(self, state):
        if state.dependencies_released:
            return
        state.dependencies_released = True
        dependencies, state.dependencies = state.dependencies, []
        for producer, port in dependencies:
            with self.store.lock:
                producer.reservations[port] = max(0, producer.reservations.get(port, 0) - 1)
                refs = self._dependency_refs(producer, port)
                self.store.release(state.session, [ref["id"] for ref in refs], lease=True)
            self._collect_call(producer)

    def _collect_call(self, state):
        if not state.forgotten or state.inflight or not state.done.done() or any(state.reservations.values()):
            return
        tokens = [ref["id"] for result in state.node_results.values() for ref in result.get("outputs", [])]
        self.store.release(state.session, tokens)
        self.calls.pop((state.session, state.call_id), None)

    async def forget_calls(self, session_id, call_ids, epoch, **_):
        if session_id in self.session_seen:
            self._session(session_id)
        if epoch == self.epoch:
            for call_id in call_ids:
                state = self.calls.get((session_id, call_id))
                if state is not None:
                    state.forgotten = True
                    self._collect_call(state)
        return {"forgotten": True}

    async def submit(self, session_id, call_id, spec, submission_window=None, **_):
        await self.start()
        self._session(session_id)
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("call_id must be a nonempty string")
        key = (session_id, call_id)
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":"),
                                           allow_nan=False).encode()).hexdigest()
        deadline = self._validate_window(session_id, submission_window)
        self._prune_submissions()
        if key in self.calls:
            if not hmac.compare_digest(self.calls[key].digest, digest):
                raise RecoveryError("SubmissionKeyConflict", "call_id was already used for another request")
            return {"call_id": call_id, "status": self.calls[key].status}
        if key in self.submissions:
            if not hmac.compare_digest(self.submissions[key][0], digest):
                raise RecoveryError("SubmissionKeyConflict", "call_id was already used for another request")
            raise RecoveryError("CallExpired", "The call was forgotten; it cannot be executed again")
        if deadline is not None and deadline <= time.time():
            raise RecoveryError("CallExpired", "The original submission window expired; execution is unknown")
        if max(len(self.submissions), len(self.calls)) >= self.recovery_policy.max_call_records:
            raise RecoveryError("RecoveryCapacity", "The call recovery capacity is full; close unused sessions")
        self.submissions[key] = (digest, deadline)
        if deadline is not None:
            heapq.heappush(self._submission_deadlines, (deadline, key))
        spec = copy.deepcopy(spec)
        done = self.loop.create_future()
        state = CallState(session_id, call_id, spec, done, digest=digest)
        if spec.get("kind") == "workflow":
            state.nodes = {name: self.loop.create_future() for name in spec["graph"]["nodes"]}
        else:
            state.nodes = {"result": self.loop.create_future()}
        self.calls[key] = state
        try:
            unique = {json.dumps(ref, sort_keys=True): ref for ref in walk_refs(spec)}
            for locator in unique.values():
                producer = self._producer(session_id, locator)
                port = self._locator_port(locator)
                if port[1] is None and producer.spec.get("kind") != "node":
                    raise ValueError("Whole-call references require an eager node producer")
                with self.store.lock:
                    producer.reservations[port] = producer.reservations.get(port, 0) + 1
                    state.dependencies.append((producer, port))
                    for ref in self._dependency_refs(producer, port):
                        self.store.acquire(session_id, ref["id"])
            state.task = asyncio.create_task(self._admit(state))
        except BaseException:
            self._release_dependencies(state)
            self.calls.pop(key)
            self.submissions.pop(key, None)
            raise
        self._emit("accepted", state)
        return {"call_id": call_id, "status": "accepted"}

    async def _locator_data(self, state, locator):
        producer = self._producer(state.session, locator)
        node_id = locator.get("node_id")
        result = self._result(await asyncio.shield(producer.nodes[node_id] if node_id is not None else producer.done))
        if locator.get("whole") is True:
            outputs = result["outputs"]
            if not outputs:
                return None
            if len(outputs) > 1:
                return [tuple(value_from_data(self.store.get(state.session, ref["id"], locator.get("epoch"), pinned=True))
                              for ref in outputs)]
            return self.store.get(state.session, outputs[0]["id"], locator.get("epoch"), pinned=True)
        token = result["outputs"][locator["slot"]]["id"]
        return self.store.get(state.session, token, locator.get("epoch"), pinned=True)

    async def _admit(self, state):
        try:
            resolved = {}
            for locator in walk_refs(state.spec):
                key = json.dumps(locator, sort_keys=True)
                if key not in resolved:
                    resolved[key] = await self._locator_data(state, locator)
            self._session(state.session, touch=False)
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
        self.store._capacity(len(entry.outputs))
        outputs = []
        for slot, data in enumerate(entry.outputs):
            is_list = slot < len(list_flags) and list_flags[slot]
            leases = sum(count for port, count in state.reservations.items()
                         if (port == (None, None) and state.spec.get("kind") == "node")
                         or (port[1] is not None and self._port(state, *port) == (node_id, slot)))
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
                    data = resolved[json.dumps(value["$ref"], sort_keys=True)]
                    return None if data is None else source(data)
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
                    data = resolved[json.dumps(value["$ref"], sort_keys=True)]
                    return None if data is None else value_from_data(data)
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
                self._session(state.session, touch=False)
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

    @staticmethod
    def _metadata(native_result):
        omitted = object()
        ancestors = set()
        remaining = 100000

        def project(value, depth=0):
            nonlocal remaining
            remaining -= 1
            if remaining < 0 or depth > 64:
                return omitted
            if value is None or type(value) in (str, bool, int):
                return value
            if type(value) is float:
                return value if math.isfinite(value) else omitted
            if type(value) not in (dict, list) or id(value) in ancestors:
                return omitted
            ancestors.add(id(value))
            try:
                if type(value) is list:
                    result = []
                    for child in value:
                        if remaining <= 0:
                            break
                        if (item := project(child, depth + 1)) is not omitted:
                            result.append(item)
                    return result
                result = {}
                for key, child in value.items():
                    if remaining <= 0:
                        break
                    if type(key) is str and (item := project(child, depth + 1)) is not omitted:
                        result[key] = item
                return result
            finally:
                ancestors.remove(id(value))

        outputs = native_result.get("history", {}).get("outputs", {})
        prompt = project(native_result.get("prompt_id"))
        executed = project(native_result.get("executed", []))
        ui = project(outputs) if type(outputs) is dict else {}
        return {"prompt_id": prompt if type(prompt) is str else None,
                "executed": executed if type(executed) is list else [],
                "ui": ui if type(ui) is dict else {}}

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
            metadata = self._metadata(native_result)
            native = {"prompt_id": metadata["prompt_id"], "executed": metadata["executed"],
                      "history": {"outputs": metadata["ui"]}}
            final = {"outputs": outputs, "names": names, "native": native}
        else:
            final = dict(self._result(state.nodes["result"].result()))
        final["metadata"] = self._metadata(native_result)
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
        payload = await self.fetch_bytes(session_id, ref_id, epoch)
        return {"codec": "pickle", "data": base64.b64encode(payload).decode()}

    async def fetch_bytes(self, session_id, ref_id, epoch, **_):
        self._session(session_id)
        value = self.store.value(session_id, ref_id, epoch)
        return cloudpickle.dumps(value)

    async def release(self, session_id, refs, epoch=None, **_):
        if session_id in self.session_seen:
            self._session(session_id)
        if epoch is None or epoch == self.epoch:
            self.store.release(session_id, refs)
        return {"released": True}

    async def close_session(self, session_id, **_):
        if self.session_seen.pop(session_id, None) is not None:
            self.closed_sessions.append(session_id)
        self.session_tokens.pop(session_id, None)
        for key in [key for key in self.submissions if key[0] == session_id]:
            self.submissions.pop(key, None)
        # Bound stale heap entries when short-lived sessions close well before
        # their signed windows expire; this also keeps churn independent of TTL.
        if len(self._submission_deadlines) > max(256, 2 * len(self.submissions)):
            self._submission_deadlines = [(deadline, key) for key, (_, deadline) in self.submissions.items()
                                          if deadline is not None]
            heapq.heapify(self._submission_deadlines)
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
        self.submissions.clear()
        self._submission_deadlines.clear()
        self.session_tokens.clear()
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        await self.queue.put(None)
        if self.worker:
            await self.worker
        if self.loop:
            await self.loop.run_in_executor(self.pool, self.adapter.close)
        if self._files is not None:
            await _io(self._files.close)
        self.pool.shutdown(wait=True)
