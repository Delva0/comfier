"""Eager calls and explicit workflow capture against one daemon session."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
import weakref
from collections.abc import Mapping
from dataclasses import dataclass

from .code import prepare_code
from .errors import InvalidReference, NotSubmitted, RemoteError, SessionClosed
from .graph import Graph, GraphInput, GraphRef, _receiver
from .protocol import PYTHON_NODE, encode_tree, lift_arguments, unpack_pickle
from .refs import Call, OutputFuture, RemoteRef, _CallState
from .transport import HTTPTransport

_NODE_INPUTS = object()


@dataclass
class _Submission:
    state: object
    kind: str
    payload: object
    artifacts: tuple


class Client:
    def __init__(self, url=None, *, transport=None, token=None):
        if transport is None and url is None:
            raise TypeError("Provide a daemon URL or transport")
        self.transport = transport or HTTPTransport(url, token=token)
        self.session_id = uuid.uuid4().hex
        self.epoch = ""
        self.handshake = {}
        self._nodes = {PYTHON_NODE: {"outputs": ["*"]}}
        self._states = weakref.WeakValueDictionary()
        self._queue = asyncio.Queue()
        self._sender = None
        self._opening = None
        self._watchers = set()
        self._uploaded = set()
        self._pending_releases = []
        self._release_task = None
        self._pending_forgets = set()
        self._forget_task = None
        self._heartbeat_task = None
        self._loop = None
        self._closed = False
        self._closing = False
        self._abort_started = False

    @property
    def closed(self):
        return self._closed

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close(wait=exc_type is None)

    def _activate(self):
        if self._closed or self._closing:
            raise SessionClosed("The client session is closed")
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("Use a client within one event loop")
        self._loop = loop
        if self._opening is None:
            self._opening = loop.create_task(self._open())
            self._opening.add_done_callback(lambda f: None if f.cancelled() else f.exception())
            self._sender = loop.create_task(self._send_loop())

    async def _open(self):
        try:
            await self.transport.open()
            self.handshake = await self.transport.request("handshake", session_id=self.session_id)
            self.session_id = self.handshake.get("session_id", self.session_id)
            self.epoch = self.handshake["epoch"]
            self._nodes.update(self.handshake.get("nodes", {}))
            self._heartbeat_task = self._loop.create_task(self._heartbeat())
        except BaseException as error:
            await self._abort_start(error)
            raise

    async def _abort_start(self, error):
        if self._abort_started:
            return
        self._abort_started = True
        self._closed = self._closing = True
        failure = error if isinstance(error, Exception) else SessionClosed("Client startup was cancelled")
        current = asyncio.current_task()
        tasks = [task for task in (self._opening, self._sender, self._heartbeat_task, *self._watchers)
                 if task is not None and task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        while not self._queue.empty():
            item = self._queue.get_nowait()
            self._queue.task_done()
            del item
        try:
            await self.transport.close()
        finally:
            for state in list(self._states.values()):
                state.fail(failure)

    async def _heartbeat(self):
        interval = min(20.0, max(0.01, self.handshake.get("session_ttl", 120) / 3))
        try:
            while True:
                await asyncio.sleep(interval)
                await self.transport.request("heartbeat", session_id=self.session_id)
                if self._pending_releases and self._release_task is None:
                    self._release_task = self._loop.create_task(self._flush_releases())
                if self._pending_forgets and self._forget_task is None:
                    self._forget_task = self._loop.create_task(self._flush_forgets())
        except asyncio.CancelledError:
            raise
        except Exception as error:
            for state in list(self._states.values()):
                state.fail(SessionClosed(f"Session heartbeat failed: {error}"))

    async def start(self):
        self._activate()
        try:
            await asyncio.shield(self._opening)
        except BaseException as error:
            await self._abort_start(error)
            raise
        return self

    def _output_count(self, node_type):
        schema = self._nodes.get(node_type, {})
        outputs = schema.get("outputs", schema.get("output"))
        return len(outputs) if isinstance(outputs, (list, tuple)) else schema.get("output_count")

    def _capture(self):
        graph = _receiver.get()
        if graph is not None and graph.client is not self:
            raise InvalidReference("A graph receiver belongs to another client")
        return graph

    def node(self, node_type, node_inputs=_NODE_INPUTS, /, **inputs):
        if node_inputs is _NODE_INPUTS:
            return self._node(node_type, inputs)
        if not isinstance(node_inputs, Mapping):
            raise TypeError("The second positional argument to node must be an input mapping")
        unknown = set(inputs) - {"extra_data"}
        if unknown:
            raise TypeError(f"Unknown node execution options: {sorted(unknown)}")
        return self._node(node_type, dict(node_inputs), extra_data=inputs.get("extra_data"))

    @staticmethod
    def _extra_data(value):
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise TypeError("extra_data must be a JSON-compatible mapping")
        return json.loads(json.dumps(dict(value), allow_nan=False))

    def _node(self, node_type, inputs, artifacts=(), *, extra_data=None):
        if not isinstance(node_type, str):
            raise TypeError("Node names must be original registry strings")
        extra_data = self._extra_data(extra_data)
        graph = self._capture()
        if graph is not None:
            if extra_data:
                raise ValueError("Execution extra_data belongs to the whole native run; pass it to submit(graph, extra_data=...) after capture")
            return graph.add(node_type, inputs, artifacts)
        self._validate_live(inputs)
        return self._enqueue("node", {"node_type": node_type, "inputs": inputs, "extra_data": extra_data},
                             artifacts, output_count=self._output_count(node_type))

    def python(self, function=None, /, *, source=None, entry=None, inputs=None, args=()):
        artifact = prepare_code(function, source=source, entry=entry)
        layout, ports = lift_arguments(tuple(args), dict(inputs or {}))
        return self._node(PYTHON_NODE, {"code": artifact.descriptor(), "layout": layout, **ports}, (artifact,))

    def graph(self):
        return Graph(self)

    def submit(self, graph, /, *, inputs=None, outputs=None, targets=None, extra_data=None):
        extra_data = self._extra_data(extra_data)
        if not isinstance(graph, Graph):
            graph = self._raw_graph(graph, outputs=outputs, targets=targets)
        elif outputs is not None or targets is not None:
            raise TypeError("Declare outputs and targets on a captured graph")
        bindings = dict(inputs or {})
        missing, extra = set(graph.inputs) - set(bindings), set(bindings) - set(graph.inputs)
        if missing or extra:
            raise ValueError(f"Invalid workflow bindings: missing={sorted(missing)}, extra={sorted(extra)}")
        capture = self._capture()
        if capture is not None:
            if extra_data:
                raise ValueError("Execution extra_data belongs to the whole native run; pass it to the outer submit after capture")
            return capture.compose(graph, bindings)
        self._validate_live(bindings)
        if not graph.sealed or graph._failed:
            raise ValueError("Submit a successfully sealed graph")
        call = self._enqueue("workflow", {"graph": graph, "bindings": bindings, "extra_data": extra_data},
                             tuple(graph.artifacts.values()), output_count=len(graph.outputs),
                             names={name: i for i, name in enumerate(graph.outputs)})
        call._graph = graph
        return call

    def _raw_graph(self, prompt, *, outputs, targets):
        if not isinstance(prompt, dict):
            raise TypeError("A raw API workflow must be a prompt dictionary")
        prompt = prompt.get("prompt", prompt)
        graph = Graph(self)
        graph.native = True
        graph.nodes = {str(key): dict(node) for key, node in prompt.items()}
        for name, source in (outputs or {}).items():
            graph.outputs[name] = GraphRef(graph, str(source[0]), int(source[1]))
        graph.targets = [str(t) for t in (targets or [])]
        graph._automatic_targets = [key for key, node in graph.nodes.items()
                                    if self._nodes.get(node["class_type"], {}).get("output_node", False)]
        graph.sealed = True
        return graph

    def _validate_live(self, value):
        if isinstance(value, (GraphRef, GraphInput)):
            raise NotSubmitted("Bind a graph port with run[port] before using it in eager calls")
        if isinstance(value, (RemoteRef, OutputFuture)):
            value._wire(self)
        elif isinstance(value, (tuple, list)):
            for item in value:
                self._validate_live(item)
        elif isinstance(value, dict):
            for item in value.values():
                self._validate_live(item)

    def _enqueue(self, kind, payload, artifacts, *, output_count=None, names=None):
        self._activate()
        state = _CallState(self, uuid.uuid4().hex, output_count=output_count, names=names)
        call = Call(state)
        self._states[(state.call_id, None)] = state
        self._queue.put_nowait(_Submission(state, kind, payload, tuple(artifacts)))
        return call

    def _encode_special(self, value):
        if isinstance(value, (RemoteRef, OutputFuture)):
            return value._wire(self)
        if isinstance(value, (GraphRef, GraphInput)):
            raise NotSubmitted("An unbound graph port is not a remote value")
        return NotImplemented

    async def _upload(self, artifacts):
        for artifact in artifacts:
            artifact.check_compatibility(self.handshake)
            if artifact.artifact_id not in self._uploaded:
                await self.transport.request("artifact_put", session_id=self.session_id,
                    artifact_id=artifact.artifact_id, kind=artifact.kind,
                    data=base64.b64encode(artifact.data).decode("ascii"))
                self._uploaded.add(artifact.artifact_id)

    async def _send_loop(self):
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                await asyncio.shield(self._opening)
                await self._upload(item.artifacts)
                if item.kind == "node":
                    spec = {"kind": "node", "node_type": item.payload["node_type"],
                            "inputs": {k: encode_tree(v, self._encode_special)
                                       for k, v in item.payload["inputs"].items()}}
                else:
                    spec = {"kind": "workflow", "graph": item.payload["graph"].to_wire(self),
                            "bindings": {k: encode_tree(v, self._encode_special)
                                         for k, v in item.payload["bindings"].items()}}
                if item.payload.get("extra_data") is not None:
                    spec["extra_data"] = item.payload["extra_data"]
                item.state.status = "submitting"
                await self.transport.request("submit", session_id=self.session_id,
                                             call_id=item.state.call_id, spec=spec)
                item.state.status = "accepted"
                if not item.state.accepted.done():
                    item.state.accepted.set_result(None)
                self._watch(item.state)
            except asyncio.CancelledError:
                if item is not None and not self._abort_started:
                    item.state.fail(SessionClosed("Submission was interrupted"))
                raise
            except Exception as error:
                if item is not None:
                    item.state.fail(error)
            finally:
                self._queue.task_done()
                del item  # transfer input ownership at ACK; do not retain the last request

    def _watch(self, state):
        task = self._loop.create_task(self._wait_result(state))
        self._watchers.add(task)
        task.add_done_callback(self._watchers.discard)

    async def _wait_result(self, state):
        try:
            await asyncio.shield(state.accepted)
            params = {"session_id": self.session_id, "call_id": state.call_id, "node_id": state.node_id}
            capabilities = self.handshake.get("capabilities", {})
            bounded = isinstance(capabilities, dict) and capabilities.get("bounded_wait") is True
            if bounded:
                params["wait_seconds"] = 20.0
            while True:
                payload = await self.transport.request("wait_result", **params)
                if payload.get("pending") is True:
                    if not bounded:
                        raise RemoteError("Daemon returned pending without advertising bounded_wait")
                    await asyncio.sleep(0)
                    continue
                state.finish(payload)
                return
        except asyncio.CancelledError:
            state.fail(SessionClosed("The client session closed before completion"))
            raise
        except Exception as error:
            state.fail(error)

    def _bind_graph_ref(self, call, ref):
        if not isinstance(ref, GraphRef) or ref.graph is not call._graph:
            raise InvalidReference("This port does not belong to the submitted workflow")
        key = (call.id, ref.node_id)
        state = self._states.get(key)
        if state is None:
            state = _CallState(self, call.id, node_id=ref.node_id, accepted=call._state.accepted,
                               lifetime=call._state.lifetime)
            self._states[key] = state
            future = OutputFuture(state.cell(ref.slot))
            self._watch(state)
            return future
        return OutputFuture(state.cell(ref.slot))

    async def _fetch(self, info):
        if self.closed:
            raise SessionClosed("The client session is closed")
        payload = await self.transport.request("fetch", session_id=self.session_id,
                                             ref_id=info["id"], epoch=info.get("epoch", self.epoch))
        return unpack_pickle(payload)

    def _release_info(self, info):
        if self._closed or self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._queue_release, dict(info))

    def _queue_release(self, info):
        if self._closed:
            return
        self._pending_releases.append(info)
        if self._release_task is None:
            self._release_task = self._loop.create_task(self._flush_releases())

    async def _flush_releases(self):
        try:
            while self._pending_releases:
                items, self._pending_releases = self._pending_releases, []
                epochs = {i.get("epoch", self.epoch) for i in items}
                for epoch in epochs:
                    await self.transport.request("release", session_id=self.session_id, epoch=epoch,
                        refs=list({i["id"] for i in items if i.get("epoch", self.epoch) == epoch}))
        except Exception:
            self._pending_releases.extend(items)  # retry on heartbeat; release is idempotent
        finally:
            self._release_task = None

    def _forget_call(self, call_id):
        if self._closed or self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._queue_forget, call_id)

    def _queue_forget(self, call_id):
        if self._closed:
            return
        self._pending_forgets.add(call_id)
        if self._forget_task is None:
            self._forget_task = self._loop.create_task(self._flush_forgets())

    async def _flush_forgets(self):
        try:
            while self._pending_forgets:
                call_ids, self._pending_forgets = self._pending_forgets, set()
                await self.transport.request("forget_calls", session_id=self.session_id,
                                             call_ids=sorted(call_ids), epoch=self.epoch)
        except Exception:
            self._pending_forgets.update(call_ids)  # retry on heartbeat; forget is idempotent
        finally:
            self._forget_task = None

    async def close(self, *, wait=True):
        if self._closed:
            return
        self._closing = True
        try:
            if self._opening is not None:
                if wait:
                    await self._queue.join()
                    while self._watchers:
                        await asyncio.gather(*tuple(self._watchers), return_exceptions=True)
                else:
                    for state in list(self._states.values()):
                        state.fail(SessionClosed("The client session closed"))
                    while not self._queue.empty():
                        item = self._queue.get_nowait()
                        self._queue.task_done()
                        del item
                try:
                    await asyncio.shield(self._opening)
                    await asyncio.sleep(0)
                    if self._release_task:
                        await self._release_task
                    if self._forget_task:
                        await self._forget_task
                    await self.transport.request("close_session", session_id=self.session_id)
                finally:
                    for task in tuple(self._watchers):
                        task.cancel()
                    if self._watchers:
                        await asyncio.gather(*tuple(self._watchers), return_exceptions=True)
                    if self._sender:
                        self._sender.cancel()
                        await asyncio.gather(self._sender, return_exceptions=True)
        finally:
            self._closed = True
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            await self.transport.close()
            self._pending_releases.clear()
            self._pending_forgets.clear()
