"""Eager calls and explicit workflow capture against one daemon session."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import json
import time
import uuid
import weakref
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial

import aiohttp

from ._outputs import _OutputProxy, declared_outputs
from .code import prepare_code
from .errors import (
    Expired,
    InvalidReference,
    NotSubmitted,
    RemoteError,
    SessionClosed,
    UnknownOutputLayout,
)
from .files import Files
from .graph import Graph, GraphInput, GraphRef, _receiver
from .management import Bash, ComfyUI, Models, Operations, Plugins
from .protocol import PYTHON_NODE, encode_tree, lift_arguments, unpack_pickle
from .refs import Call, OutputFuture, RemoteRef, _CallState
from .transport import HTTPTransport, retryable_transport_error

_NODE_INPUTS = object()
_FETCH_COMPRESSION = object()


def _call_weak(callback_ref, *args):
    callback = callback_ref()
    if callback is not None:
        callback(*args)


class _ClientCleanup:
    """GC cleanup state: no member or task may point back to the Client."""

    def __init__(self, transport):
        self.transport = transport
        self.loop = self.session_id = self.lease = None
        self.tasks = set()
        self.session_close_confirmed = True

    def abandon(self):
        if self.lease is not None:
            self.lease.stop()
        if self.loop is not None and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self._schedule, context=contextvars.Context())
            except RuntimeError:
                pass

    def _schedule(self):
        task = self.loop.create_task(self._dispose(), context=contextvars.Context())
        task.add_done_callback(lambda result: None if result.cancelled() else result.exception(),
                               context=contextvars.Context())

    async def _dispose(self):
        tasks = [task for task in self.tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            if self.lease is not None:
                await self.lease.close()
            if self.session_id is not None:
                self.session_close_confirmed = False
                async with asyncio.timeout(2):
                    await self.transport.request("close_session", session_id=self.session_id)
                self.session_close_confirmed = True
        except Exception:
            pass  # A disconnected server still expires the existing lease.
        finally:
            await self.transport.close()


def _heartbeat_notice(client_ref, error=None, terminal=False):
    client = client_ref()
    if client is None or client._closed:
        return
    # Keep application traceback frames out of the thread's error snapshot.
    if error is not None:
        error = type(error)(str(error), details=error.details)
    client._heartbeat_error = error
    if terminal and not client._closing:
        if client._managed:
            client._expire_compute(error)
            return
        task = client._loop.create_task(client._abort_start(error), context=contextvars.Context())
        client._abort_task = task
        task.add_done_callback(lambda result: None if result.cancelled() else result.exception(),
                               context=contextvars.Context())
    elif error is None:
        if client._pending_releases and client._release_task is None:
            client._release_task = client._task(client._flush_releases(client_ref))
        if client._pending_forgets and client._forget_task is None:
            client._forget_task = client._task(client._flush_forgets(client_ref))


@dataclass
class _Submission:
    state: object
    kind: str
    payload: object
    artifacts: tuple


class _ConnectionRecovery:
    """Same-session recovery state, deliberately without a Client back-reference."""

    def __init__(self, transport, handshake, session_id):
        self.transport, self.session_id = transport, session_id
        self.epoch = handshake.get("epoch")
        self.token = handshake.get("resume_token")
        self.enabled = handshake.get("capabilities", {}).get("session_resume") == 1 and bool(self.token)
        self.lock = asyncio.Lock()
        self.generation = 0
        self.submission_window = handshake.get("submission_window")
        self.reconnecting = False

    async def request(self, method, **params):
        delay = 0.05
        while True:
            generation = self.generation
            try:
                result = await self.transport.request(method, **params)
                self.reconnecting = False
                return result
            except Exception as error:
                if not self.enabled or not retryable_transport_error(error):
                    raise
                self.reconnecting = True
            async with self.lock:
                if generation != self.generation:
                    continue
                while True:
                    try:
                        handshake = await self.transport.request("resume", session_id=self.session_id,
                            resume_token=self.token, epoch=self.epoch)
                        if handshake.get("epoch") != self.epoch:
                            from .errors import Expired
                            raise Expired("The runtime restarted; old computation references expired")
                        self.generation += 1
                        self.reconnecting = False
                        break
                    except Exception as error:
                        if not retryable_transport_error(error):
                            raise
                        await asyncio.sleep(delay)
                        delay = min(2.0, delay * 2)


class Client:
    def __init__(self, url=None, *, transport=None, token=None,
                 fetch_compression=_FETCH_COMPRESSION):
        if transport is None and url is None:
            raise TypeError("Provide a daemon URL or transport")
        if fetch_compression is not _FETCH_COMPRESSION:
            if fetch_compression not in ("auto", "identity"):
                raise ValueError("fetch_compression must be 'auto' or 'identity'")
            if transport is not None:
                raise TypeError("Set fetch_compression on HTTPTransport when supplying a transport")
        compression = "auto" if fetch_compression is _FETCH_COMPRESSION else fetch_compression
        self.transport = transport or HTTPTransport(url, token=token, fetch_compression=compression)
        self.session_id = uuid.uuid4().hex
        self.epoch = ""
        self.handshake = {}
        self.management_handshake = {}
        self._managed = False
        self._compute_opening = None
        self._recovery = None
        self._nodes = {PYTHON_NODE: {"outputs": ["*"]}}
        self._node_metadata_epoch = None
        self._states = weakref.WeakValueDictionary()
        self._queue = asyncio.Queue()
        self._sender = None
        self._opening = None
        self._watchers = set()
        self._file_tasks = set()
        self._uploaded = set()
        self._pending_releases = []
        self._release_task = None
        self._pending_forgets = set()
        self._forget_task = None
        self._heartbeat_task = None
        self._heartbeat_error = None
        self._lease = None
        self._loop = None
        self._closed = False
        self._closing = False
        self._abort_started = False
        self._abort_task = None
        self._close_task = None
        self._cleanup = _ClientCleanup(self.transport)
        self._finalizer = weakref.finalize(self, self._cleanup.abandon)

    @property
    def closed(self):
        return self._closed

    @property
    def close_confirmed(self):
        """False after an offline close whose remote revocation was unconfirmed."""
        return self._cleanup.session_close_confirmed

    @property
    def files(self) -> Files:
        return Files(self)

    @property
    def bash(self) -> Bash:
        return Bash(self)

    @property
    def comfyui(self) -> ComfyUI:
        return ComfyUI(self)

    @property
    def plugins(self) -> Plugins:
        return Plugins(self)

    @property
    def models(self) -> Models:
        return Models(self)

    @property
    def operations(self) -> Operations:
        return Operations(self)

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close(wait=exc_type is None)

    def _activate(self):
        if self._closed or self._closing:
            if isinstance(self._heartbeat_error, SessionClosed):
                raise SessionClosed(str(self._heartbeat_error), details=self._heartbeat_error.details)
            raise SessionClosed("The client session is closed")
        if not self._managed:
            self._check_heartbeat()
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("Use a client within one event loop")
        self._loop = loop
        self._cleanup.loop = loop
        if self._opening is None:
            self._opening = self._task(self._open())
            self._opening.add_done_callback(lambda f: None if f.cancelled() else f.exception(),
                                            context=contextvars.Context())
            self._sender = self._task(self._send_loop(weakref.ref(self), self._queue, self._opening))

    def _task(self, coroutine):
        task = self._loop.create_task(coroutine, context=contextvars.Context())
        self._cleanup.tasks.add(task)
        task.add_done_callback(self._cleanup.tasks.discard, context=contextvars.Context())
        return task

    def _check_heartbeat(self):
        # A failed renewal is not proof that the server lease has expired.
        # Let business RPCs proceed until the keeper receives a terminal error.
        if self._lease is not None and not self._lease.terminal:
            return
        error = self._lease.error if self._lease is not None else self._heartbeat_error
        if error is not None:
            raise type(error)(str(error), details=error.details) from None

    async def _open(self):
        try:
            await self.transport.open()
            if isinstance(self.transport, HTTPTransport) or getattr(self.transport, "supports_management", False):
                try:
                    self.management_handshake = await self.transport.management_request("handshake")
                except aiohttp.ClientResponseError as error:
                    if error.status != 404:
                        raise
                else:
                    self._managed = True
                    self.handshake = self.management_handshake
                    self.session_id = self.handshake.get("session_id", self.session_id)
                    self._cache_node_metadata(self.handshake.get("node_metadata"))
                    return
            await self._open_compute()
        except BaseException as error:
            await self._abort_start(error)
            raise

    async def _attach(self):
        await asyncio.shield(self._opening)
        if self._managed:
            opening = self._compute_opening
            # A failed attachment accepted no computation. A later call may
            # retry against a runtime explicitly started by another client.
            if opening is None or (opening.done() and
                    (opening.cancelled() or opening.exception() is not None)):
                self._compute_opening = self._task(self._open_compute())
                self._compute_opening.add_done_callback(
                    lambda task: None if task.cancelled() else task.exception(),
                    context=contextvars.Context())
            await asyncio.shield(self._compute_opening)

    def _expire_compute(self, error):
        for state in list(self._states.values()):
            state.invalidated = error
            state.fail(error)

    async def _reset_compute(self):
        """An explicit lifecycle change permits a fresh compute epoch on next use."""
        if self._cleanup.session_id is not None:
            self._expire_compute(Expired("The runtime lifecycle changed; old references expired"))
        if self._lease is not None:
            await self._lease.close()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
        self._lease = self._cleanup.lease = None
        self._heartbeat_task = None
        self._heartbeat_error = None
        self._compute_opening = None
        self._recovery = None
        self._uploaded.clear()
        self._nodes = {PYTHON_NODE: {"outputs": ["*"]}}
        self._node_metadata_epoch = None
        self.session_id = self.management_handshake.get("session_id", self.session_id)
        self.handshake = self.management_handshake
        self.epoch = ""
        self._cleanup.session_id = None
        self._cleanup.session_close_confirmed = True
        if isinstance(self.transport, HTTPTransport):
            self.transport._reset_runtime_capabilities()

    async def _open_compute(self):
        try:
            self.handshake = await self.transport.request("handshake", session_id=self.session_id)
            if isinstance(self.transport, HTTPTransport) and not (
                    isinstance(self.handshake.get("session_id"), str) and self.handshake["session_id"]):
                raise RemoteError("Daemon handshake did not return a server-issued session_id")
            self.session_id = self.handshake.get("session_id", self.session_id)
            self._cleanup.session_id = self.session_id
            self.epoch = self.handshake["epoch"]
            self._recovery = _ConnectionRecovery(self.transport, self.handshake, self.session_id)
            self._cache_node_metadata({"epoch": self.epoch, "nodes": self.handshake.get("nodes", {})})
            interval = min(20.0, max(0.01, self.handshake.get("session_ttl", 120) / 3))
            if isinstance(self.transport, HTTPTransport) and self.handshake.get("session_id"):
                self._lease = self.transport._start_heartbeat(self.session_id, interval, self._loop,
                    partial(_heartbeat_notice, weakref.ref(self)))
                self._cleanup.lease = self._lease
                await asyncio.wrap_future(self._lease.ready)
            else:
                self._heartbeat_task = self._task(self._heartbeat(
                    weakref.ref(self), self.transport, self.session_id, interval))
        except BaseException as error:
            if not self._managed:
                await self._abort_start(error)
            raise

    async def _abort_start(self, error):
        if self._abort_started:
            return
        self._abort_started = True
        self._closed = self._closing = True
        failure = error if isinstance(error, Exception) else SessionClosed("Client startup was cancelled")
        current = asyncio.current_task()
        tasks = [task for task in tuple(self._cleanup.tasks)
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
            await self._cleanup._dispose()
        finally:
            self._finalizer.detach()
            for state in list(self._states.values()):
                state.fail(failure)

    @staticmethod
    async def _heartbeat(client_ref, transport, session_id, interval):
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await transport.request("heartbeat", session_id=session_id)
                except Exception as error:
                    if not retryable_transport_error(error):
                        raise
                    _heartbeat_notice(client_ref, RemoteError(f"Session heartbeat unavailable: {error}"))
                else:
                    _heartbeat_notice(client_ref)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _heartbeat_notice(client_ref, SessionClosed(f"Session heartbeat failed: {error}"), True)

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
        if schema.get("output_layout", "fixed") != "fixed":
            return None
        if "output_count" in schema:
            count = schema["output_count"]
            return count if type(count) is int and count >= 0 else None
        outputs = schema.get("outputs", schema.get("output"))
        return len(outputs) if isinstance(outputs, (list, tuple)) else schema.get("output_count")

    def _cache_node_metadata(self, metadata):
        if metadata is None:
            self._nodes = {PYTHON_NODE: {"outputs": ["*"]}}
            self._node_metadata_epoch = None
            return
        if not isinstance(metadata, dict) or not isinstance(metadata.get("nodes"), dict):
            raise RemoteError("Daemon returned invalid node metadata")
        epoch = metadata.get("epoch")
        if not isinstance(epoch, str) or not epoch:
            raise RemoteError("Daemon returned node metadata without a runtime epoch")
        self._nodes = {PYTHON_NODE: {"outputs": ["*"]}, **metadata["nodes"]}
        self._node_metadata_epoch = epoch

    async def _node_layout(self, node_type):
        """Read one node's layout from the attached runtime, without submitting it."""
        self._activate()
        await self._attach()
        self._check_heartbeat()
        return self._output_count(node_type), self._nodes.get(node_type, {})

    def _capture(self):
        graph = _receiver.get()
        if graph is not None and graph.client is not self:
            raise InvalidReference("A graph receiver belongs to another client")
        return graph

    def node(self, node_type, node_inputs=_NODE_INPUTS, /, *, return_call=False,
             declared_outputs=True, **inputs):
        if type(return_call) is not bool or type(declared_outputs) is not bool:
            raise TypeError("return_call and declared_outputs must be bools")
        if node_inputs is _NODE_INPUTS:
            return self._node(node_type, inputs, return_call=return_call, check=declared_outputs)
        if not isinstance(node_inputs, Mapping):
            raise TypeError("The second positional argument to node must be an input mapping")
        unknown = set(inputs) - {"extra_data"}
        if unknown:
            raise TypeError(f"Unknown node execution options: {sorted(unknown)}")
        return self._node(node_type, dict(node_inputs), extra_data=inputs.get("extra_data"),
                          return_call=return_call, check=declared_outputs)

    @staticmethod
    def _extra_data(value):
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise TypeError("extra_data must be a JSON-compatible mapping")
        return json.loads(json.dumps(dict(value), allow_nan=False))

    def _node(self, node_type, inputs, artifacts=(), *, extra_data=None, return_call=True, check=None):
        if not isinstance(node_type, str):
            raise TypeError("Node names must be original registry strings")
        extra_data = self._extra_data(extra_data)
        graph = self._capture()
        count = self._output_count(node_type)
        if check and not return_call and count is None:
            raise UnknownOutputLayout("No fixed node declaration is cached; await comfyui.start() or comfyui.info(), or use declared_outputs=False")
        if graph is not None:
            if extra_data:
                raise ValueError("Execution extra_data belongs to the whole native run; pass it to submit(graph, extra_data=...) after capture")
            if check:
                graph._bind_metadata_epoch(self._node_metadata_epoch)
            call = graph.add(node_type, inputs, artifacts, check=check)
        else:
            self._validate_live(inputs)
            call = self._enqueue("node", {"node_type": node_type, "inputs": inputs, "extra_data": extra_data},
                                 artifacts, output_count=count, declared_count=count,
                                 check_outputs=bool(check),
                                 metadata_epoch=self._node_metadata_epoch if check else None)
        if return_call:
            return call
        return declared_outputs(call, count, graph=graph) if check else _OutputProxy(call, graph=graph)

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
        if isinstance(value, (RemoteRef, OutputFuture, _OutputProxy)):
            value._wire(self)
        elif isinstance(value, (tuple, list)):
            for item in value:
                self._validate_live(item)
        elif isinstance(value, dict):
            for item in value.values():
                self._validate_live(item)

    def _enqueue(self, kind, payload, artifacts, *, output_count=None, declared_count=None,
                 check_outputs=False, metadata_epoch=None, names=None):
        self._activate()
        state = _CallState(self, uuid.uuid4().hex, output_count=output_count,
                           declared_count=declared_count, check_outputs=check_outputs, names=names)
        state.epoch = metadata_epoch or self.epoch
        call = Call(state)
        self._states[(state.call_id, None)] = state
        self._queue.put_nowait(_Submission(state, kind, payload, tuple(artifacts)))
        return call

    def _encode_special(self, value):
        if (isinstance(value, _OutputProxy)
                and self.handshake.get("capabilities", {}).get("whole_call_refs") != 1):
            raise RemoteError("This daemon does not support whole node-result inputs")
        if isinstance(value, (RemoteRef, OutputFuture, _OutputProxy)):
            return value._wire(self)
        if isinstance(value, (GraphRef, GraphInput)):
            raise NotSubmitted("An unbound graph port is not a remote value")
        return NotImplemented

    @staticmethod
    async def _upload(transport, handshake, uploaded, session_id, artifacts):
        for artifact in artifacts:
            artifact.check_compatibility(handshake)
            if artifact.artifact_id not in uploaded:
                await transport.request("artifact_put", session_id=session_id,
                    artifact_id=artifact.artifact_id, kind=artifact.kind,
                    data=base64.b64encode(artifact.data).decode("ascii"))
                uploaded.add(artifact.artifact_id)

    @staticmethod
    async def _send_loop(client_ref, queue, opening):
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                await asyncio.shield(opening)
                client = client_ref()
                if client is None:
                    return
                client._check_heartbeat()
                await client._attach()
                handshake, uploaded = client.handshake, client._uploaded
                recovery = client._recovery
                session_id = client.session_id
                item.state.client()
                if item.state.check_outputs and item.state.declared_count is None and item.kind == "node":
                    item.state.declared_count = client._output_count(item.payload["node_type"])
                if item.kind == "node":
                    spec = {"kind": "node", "node_type": item.payload["node_type"],
                            "inputs": {k: encode_tree(v, client._encode_special)
                                       for k, v in item.payload["inputs"].items()}}
                else:
                    spec = {"kind": "workflow", "graph": item.payload["graph"].to_wire(client),
                            "bindings": {k: encode_tree(v, client._encode_special)
                                         for k, v in item.payload["bindings"].items()}}
                del client
                await Client._upload(recovery, handshake, uploaded, session_id, item.artifacts)
                if item.payload.get("extra_data") is not None:
                    spec["extra_data"] = item.payload["extra_data"]
                item.state.status = "submitting"
                item.state.epoch = recovery.epoch
                submission = {"session_id": session_id, "call_id": item.state.call_id, "spec": spec}
                if recovery.submission_window is not None:
                    window = recovery.submission_window
                    if window.get("expires_at", 0) <= time.time() + 5:
                        value = await recovery.request("submission_window", session_id=session_id,
                                                       epoch=recovery.epoch)
                        window = value.get("submission_window", value.get("window", value))
                        recovery.submission_window = window
                    submission["submission_window"] = dict(window)
                await recovery.request("submit", **submission)
                item.state.status = "accepted"
                if not item.state.accepted.done():
                    item.state.accepted.set_result(None)
                client = client_ref()
                if client is not None:
                    client._watch(item.state)
                del client
            except asyncio.CancelledError:
                client = client_ref()
                if item is not None and (client is None or not client._abort_started):
                    item.state.fail(SessionClosed("Submission was interrupted"))
                del client
                raise
            except Exception as error:
                if item is not None:
                    item.state.fail(error)
            finally:
                client = None
                queue.task_done()
                del item  # transfer input ownership at ACK; do not retain the last request

    def _watch(self, state):
        task = self._task(self._wait_result(state))
        self._watchers.add(task)
        task.add_done_callback(self._watchers.discard, context=contextvars.Context())

    @staticmethod
    async def _wait_result(state):
        try:
            await asyncio.shield(state.accepted)
            client = state.client()
            transport, session_id = client._recovery, client.session_id
            capabilities = client.handshake.get("capabilities", {})
            bounded = isinstance(capabilities, dict) and capabilities.get("bounded_wait") is True
            state.epoch = client.epoch
            del client
            params = {"session_id": session_id, "call_id": state.call_id, "node_id": state.node_id}
            if bounded:
                params["wait_seconds"] = 20.0
            while True:
                payload = await transport.request("wait_result", **params)
                if payload.get("pending") is True:
                    if not bounded:
                        raise RemoteError("Daemon returned pending without advertising bounded_wait")
                    await asyncio.sleep(0)
                    continue
                state.finish(payload)
                return
        except asyncio.CancelledError:
            client = state.client_ref()
            aborting = client is not None and client._abort_started
            del client
            if not aborting:
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
        self._check_heartbeat()
        payload = await self._recovery.request("fetch", session_id=self.session_id,
                                             ref_id=info["id"], epoch=info.get("epoch", self.epoch))
        return unpack_pickle(payload)

    def _release_info(self, info):
        if self._closed or self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(_call_weak, weakref.WeakMethod(self._queue_release), dict(info),
                                       context=contextvars.Context())

    def _queue_release(self, info):
        if self._closed:
            return
        self._pending_releases.append(info)
        if self._release_task is None:
            self._release_task = self._task(self._flush_releases(weakref.ref(self)))

    @staticmethod
    async def _flush_releases(client_ref):
        client = client_ref()
        try:
            while client is not None and client._pending_releases:
                items, client._pending_releases = client._pending_releases, []
                transport, session_id, current_epoch = client._recovery, client.session_id, client.epoch
                del client
                epochs = {i.get("epoch", current_epoch) for i in items}
                for epoch in epochs:
                    await transport.request("release", session_id=session_id, epoch=epoch,
                        refs=list({i["id"] for i in items if i.get("epoch", current_epoch) == epoch}))
                client = client_ref()
        except Exception:
            client = client_ref()
            if client is not None:
                client._pending_releases.extend(items)  # retry on heartbeat; release is idempotent
        finally:
            client = client_ref()
            if client is not None:
                client._release_task = None

    def _forget_call(self, call_id):
        if self._closed or self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(_call_weak, weakref.WeakMethod(self._queue_forget), call_id,
                                       context=contextvars.Context())

    def _queue_forget(self, call_id):
        if self._closed:
            return
        self._pending_forgets.add(call_id)
        if self._forget_task is None:
            self._forget_task = self._task(self._flush_forgets(weakref.ref(self)))

    @staticmethod
    async def _flush_forgets(client_ref):
        client = client_ref()
        try:
            while client is not None and client._pending_forgets:
                call_ids, client._pending_forgets = client._pending_forgets, set()
                transport, session_id, epoch = client._recovery, client.session_id, client.epoch
                del client
                await transport.request("forget_calls", session_id=session_id,
                                        call_ids=sorted(call_ids), epoch=epoch)
                client = client_ref()
        except Exception:
            client = client_ref()
            if client is not None:
                client._pending_forgets.update(call_ids)  # retry on heartbeat; forget is idempotent
        finally:
            client = client_ref()
            if client is not None:
                client._forget_task = None

    async def close(self, *, wait=True):
        if self._abort_task is not None:
            await asyncio.shield(self._abort_task)
        if self._close_task is not None:
            await asyncio.shield(self._close_task)
            return
        if self._closed:
            return
        self._closing = True
        for task in tuple(self._file_tasks):
            task.cancel()
        self._close_task = asyncio.create_task(self._close(wait=wait))
        self._close_task.add_done_callback(lambda result: None if result.cancelled() else result.exception(),
                                           context=contextvars.Context())
        await asyncio.shield(self._close_task)

    async def _close(self, *, wait):
        self._closing = True
        if self._recovery is not None and self._recovery.reconnecting:
            wait = False
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
            try:
                await self._cleanup._dispose()
            finally:
                self._finalizer.detach()
                self._pending_releases.clear()
                self._pending_forgets.clear()
