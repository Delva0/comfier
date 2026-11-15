"""Submission futures and ownership. A ready reference is deliberately not awaitable."""

from __future__ import annotations

import asyncio
import contextvars
import weakref
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from .errors import Expired, InvalidReference, Released, SessionClosed, error_from_wire

T = TypeVar("T")


@dataclass(frozen=True)
class CallMetadata:
    """Local native UI facts, without the submitted prompt or hidden extra_data."""

    call_id: str
    prompt_id: str | None = None
    executed: tuple[str, ...] = ()
    ui: dict = field(default_factory=dict)


def _drop_owner(client_ref, record):
    if record["released"]:
        return
    record["released"] = True
    info = record.get("info")
    client = client_ref()
    if client is not None and info and info.get("owner", "owned") == "owned":
        client._release_info(info)


def _drop_invocation(client_ref, call_id):
    client = client_ref()
    if client is not None:
        client._forget_call(call_id)


class _InvocationLifetime:
    def __init__(self, client, call_id):
        self.finalizer = weakref.finalize(self, _drop_invocation, weakref.ref(client), call_id)


class _OwnerCell:
    def __init__(self, state, slot):
        self.state = state
        self._client = state.client_ref()
        self.slot = slot
        self.record = {"released": False, "info": None}
        self.ready_ref = None
        self.finalizer = weakref.finalize(self, _drop_owner, state.client_ref, self.record)

    def check(self):
        if self.record["released"]:
            raise Released("This output lease was released")

    def release(self):
        try:
            self.finalizer()
        finally:
            self._client = None


class _CallState:
    def __init__(self, client, call_id, *, output_count=None, names=None, node_id=None, accepted=None,
                 lifetime=None):
        self.client_ref = weakref.ref(client)
        self.call_id = call_id
        self.epoch = ""
        self.invalidated = None
        self.metadata = None
        self.node_id = node_id
        self.lifetime = lifetime if lifetime is not None else _InvocationLifetime(client, call_id)
        self.output_count = output_count
        self.names = names or {}
        self.status = "queued"
        self.done = asyncio.get_running_loop().create_future()
        self.done.add_done_callback(lambda f: None if f.cancelled() else f.exception(),
                                    context=contextvars.Context())
        self.accepted = accepted if accepted is not None else asyncio.get_running_loop().create_future()
        self.accepted.add_done_callback(lambda f: None if f.cancelled() else f.exception(),
                                        context=contextvars.Context())
        self.cells = weakref.WeakValueDictionary()
        self.call_ref = lambda: None

    def client(self):
        client = self.client_ref()
        if client is None or client.closed:
            raise SessionClosed("The client session is closed")
        if self.invalidated is not None:
            raise type(self.invalidated)(str(self.invalidated), details=self.invalidated.details)
        if self.epoch and client.epoch and self.epoch != client.epoch:
            raise Expired("This reference belongs to a previous runtime epoch")
        return client

    def slot(self, selector):
        if isinstance(selector, str):
            try:
                return self.names[selector]
            except KeyError:
                raise KeyError(f"Unknown output name {selector!r}") from None
        if not isinstance(selector, int) or isinstance(selector, bool) or selector < 0:
            raise IndexError("An output slot must be a nonnegative integer")
        if self.done.done() and self.done.exception() is None and self.output_count is not None and selector >= self.output_count:
            raise IndexError(f"Output {selector} is outside {self.output_count} output slots")
        return selector

    def cell(self, slot):
        cell = self.cells.get(slot)
        if cell is None:
            cell = _OwnerCell(self, slot)
            self.cells[slot] = cell
            if self.done.done() and not self.done.cancelled() and self.done.exception() is None:
                outputs = self.done.result()
                if slot >= len(outputs):
                    raise IndexError(f"Producer did not return output {slot}")
                cell.record["info"] = outputs[slot]
        return cell

    def finish(self, payload):
        if self.done.done():
            client = self.client_ref()
            if client is not None:
                for info in payload.get("outputs", []):
                    if info.get("owner", "owned") == "owned":
                        client._release_info(info)
            return
        if payload.get("error"):
            self.fail(error_from_wire(payload["error"]))
            return
        outputs = payload.get("outputs", [])
        metadata = payload.get("metadata", {})
        self.metadata = CallMetadata(self.call_id, metadata.get("prompt_id"),
                                     tuple(metadata.get("executed", ())), metadata.get("ui", {}))
        self.output_count = len(outputs)
        self.names.update(payload.get("names", {}))
        call = self.call_ref()
        if call is not None:
            call._hold_outputs()
        client = self.client_ref()
        for slot, info in enumerate(outputs):
            cell = self.cells.get(slot)
            if cell is None or cell.record["released"]:
                if client is not None and info.get("owner", "owned") == "owned":
                    client._release_info(info)
            else:
                cell.record["info"] = info
        self.status = "completed"
        self.done.set_result(outputs)

    def fail(self, error):
        if not self.accepted.done():
            self.accepted.set_exception(error)
        if not self.done.done():
            self.status = "failed"
            self.done.set_exception(error)

    def wire_ref(self, slot):
        client = self.client()
        return {"$ref": {"session": client.session_id, "epoch": self.epoch or client.epoch,
                         "call_id": self.call_id, "slot": slot,
                         **({"node_id": self.node_id} if self.node_id is not None else {})}}


class OutputFuture(Generic[T]):
    def __init__(self, cell):
        self._cell = cell

    @property
    def slot(self):
        return self._cell.slot

    def __await__(self):
        return self._ready().__await__()

    async def _ready(self):
        self._cell.check()
        await asyncio.shield(self._cell.state.done)
        self._cell.check()
        if self.slot >= self._cell.state.output_count:
            raise IndexError(f"Producer did not return output {self.slot}")
        ref = self._cell.ready_ref() if self._cell.ready_ref else None
        if ref is None:
            ref = RemoteRef(self._cell)
            self._cell.ready_ref = weakref.ref(ref)
        return ref

    def release(self):
        self._cell.release()

    def _wire(self, client):
        self._cell.check()
        if self._cell.state.client_ref() is not client:
            raise InvalidReference("References cannot cross client sessions")
        return self._cell.state.wire_ref(self.slot)

    def __reduce__(self):
        raise TypeError("Pass remote references through inputs; do not pickle them into code or objects")


class RemoteRef(Generic[T]):
    def __init__(self, cell):
        self._cell = cell

    @property
    def id(self):
        self._cell.check()
        return self._cell.record["info"]["id"]

    @property
    def ownership(self):
        return self._cell.record["info"].get("owner", "owned")

    async def fetch(self):
        self._cell.check()
        client = self._cell.state.client()
        return await client._fetch(self._cell.record["info"])

    def release(self):
        self._cell.release()

    def _wire(self, client):
        return OutputFuture(self._cell)._wire(client)

    def __bool__(self):
        raise TypeError("Fetch the value before using a remote reference in local control flow")

    def __reduce__(self):
        raise TypeError("Pass remote references through inputs; do not pickle them into code or objects")


class Call(Generic[T]):
    def __init__(self, state):
        self._state = state
        self._client = state.client_ref()
        self._outputs = {}
        self._graph = None
        state.call_ref = weakref.ref(self)
        self._hold_outputs()

    @property
    def id(self):
        return self._state.call_id

    @property
    def status(self):
        return self._state.status

    async def metadata(self) -> CallMetadata:
        """Wait for this producer and return local JSON UI/execution metadata."""
        await asyncio.shield(self._state.done)
        return self._state.metadata

    def _hold_outputs(self):
        if self._state.output_count is not None:
            for slot in range(self._state.output_count):
                self.output(slot)

    def output(self, selector=0):
        slot = self._state.slot(selector)
        if slot not in self._outputs:
            self._outputs[slot] = OutputFuture(self._state.cell(slot))
        return self._outputs[slot]

    def __getitem__(self, graph_ref):
        if self._graph is None:
            raise TypeError("Only workflow calls can bind graph references")
        return self._state.client()._bind_graph_ref(self, graph_ref)

    def __await__(self):
        return self._ready().__await__()

    async def _ready(self):
        await asyncio.shield(self._state.done)
        return tuple([await self.output(i) for i in range(self._state.output_count)])
