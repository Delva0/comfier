"""Optional authenticated compute RPC over one IPC/TCP multipart protocol."""

from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import stat
from pathlib import Path

from comani._zmq_protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    METHODS,
    VERSION,
    capabilities,
    curve_key,
    dependency,
    endpoint,
    limit,
    positive,
)
from comani.errors import SessionClosed, error_to_wire


class ZMQServer:
    """Own sockets only; the caller owns the runtime and its session leases.

    A single ROUTER may bind both endpoint kinds. Authentication is required on
    every request. Remote TCP additionally requires CURVE; the token is carried
    inside that encrypted connection, never in endpoint discovery or logs.
    """

    def __init__(self, runtime, *, endpoints, token, timeout=60, max_inflight=64,
                 max_request_bytes=MAX_REQUEST_BYTES, max_response_bytes=MAX_RESPONSE_BYTES,
                 curve_public_key=None, curve_secret_key=None):
        if not isinstance(token, str) or not token:
            raise ValueError("ZMQ compute requires a nonempty authentication token")
        curve_public_key = curve_key(curve_public_key, "curve_public_key")
        curve_secret_key = curve_key(curve_secret_key, "curve_secret_key")
        if bool(curve_public_key) != bool(curve_secret_key):
            raise ValueError("CURVE requires both server public and secret keys")
        self.runtime, self._token = runtime, token
        self.timeout = positive(timeout, "timeout")
        self.max_inflight = limit(max_inflight, "max_inflight")
        self.max_request_bytes = limit(max_request_bytes, "max_request_bytes")
        self.max_response_bytes = limit(max_response_bytes, "max_response_bytes")
        self._bind = tuple(endpoint(item, encrypted=bool(curve_secret_key), bind=True) for item in endpoints)
        if not self._bind:
            raise ValueError("Provide at least one ZMQ bind endpoint")
        self.curve_public_key, self._curve_secret_key = curve_public_key, curve_secret_key
        self.endpoints = []
        self._ipc_files = {}
        self._context = self._socket = self._reader = None
        self._active = set()
        self._sending = asyncio.Lock()
        self._counts = {"control": 0, "wait": 0, "data": 0}

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, *args):
        await self.close()

    async def start(self):
        if self._socket is not None:
            return self
        zmq = dependency()
        self._zmq = zmq
        self._context = zmq.asyncio.Context()
        self._socket = self._context.socket(zmq.ROUTER)
        for option, value in ((zmq.LINGER, 0), (zmq.SNDHWM, self.max_inflight),
                              (zmq.RCVHWM, self.max_inflight), (zmq.ROUTER_MANDATORY, 1),
                              (zmq.MAXMSGSIZE, self.max_request_bytes)):
            self._socket.setsockopt(option, value)
        try:
            if self._curve_secret_key:
                from zmq.utils.z85 import encode

                if len(self.curve_public_key) == 32:
                    self.curve_public_key = encode(self.curve_public_key)
                self._socket.curve_publickey = self.curve_public_key
                self._socket.curve_secretkey = self._curve_secret_key
                self._socket.curve_server = True
            for item in self._bind:
                self._socket.bind(item)
                bound = self._socket.getsockopt_string(zmq.LAST_ENDPOINT)
                self.endpoints.append(bound)
                if bound.startswith("ipc://"):
                    path = Path(bound[6:])
                    observed = path.lstat()
                    self._ipc_files[path] = (observed.st_dev, observed.st_ino)
            self._reader = asyncio.create_task(self._receive())
        except BaseException:
            await self.close()
            raise
        return self

    async def _receive(self):
        while True:
            try:
                # Idle sockets also yield periodically; no receive waits forever.
                async with asyncio.timeout(self.timeout):
                    frames = await self._socket.recv_multipart()
            except TimeoutError:
                continue
            if len(frames) != 3:
                continue
            identity, request_id, body = frames
            if len(request_id) > 64:
                continue
            try:
                if len(body) > self.max_request_bytes:
                    raise ValueError("Compute request exceeds the configured byte limit")
                request = json.loads(body)
                if not isinstance(request, dict):
                    raise ValueError("Expected a compute request object")
                supplied = request.get("token")
                if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), self._token.encode()):
                    await self._reply(identity, request_id, [{"error": {
                        "code": "Unauthorized", "message": "Invalid runtime token"}}])
                    continue
                method, params = request.get("method"), request.get("params", {})
                if request.get("version") != VERSION or method not in METHODS or not isinstance(params, dict):
                    raise ValueError("Unknown compute RPC method or protocol version")
                if params.get("session_id") == "raw":
                    raise SessionClosed("The native prompt channel is reserved; open a client session")
                deadline = min(self.timeout, positive(request.get("timeout", self.timeout), "timeout"))
                lane = "wait" if method == "wait_result" else "data" if method in {"fetch", "artifact_put"} else "control"
                ceiling = max(1, self.max_inflight // 2) if lane == "wait" else max(1, self.max_inflight // 4) if lane == "data" else self.max_inflight
                if len(self._active) >= self.max_inflight or self._counts[lane] >= ceiling:
                    await self._reply(identity, request_id, [{"error": {
                        "code": "TransportBusy", "message": "Compute request capacity is full"}, "retryable": True}])
                    continue
                self._counts[lane] += 1
                task = asyncio.create_task(self._handle(identity, request_id, method, params, deadline, lane))
                self._active.add(task)
                task.add_done_callback(self._active.discard)
            except Exception as error:
                await self._reply(identity, request_id, [{"error": error_to_wire(error)}])

    async def _handle(self, identity, request_id, method, params, deadline, lane):
        try:
            async with asyncio.timeout(deadline):
                if method == "wait_result":
                    params = dict(params)
                    params["wait_seconds"] = min(params.get("wait_seconds") or 10, deadline / 2)
                proxy = getattr(self.runtime, "runtime_request", None)
                if callable(proxy):
                    result = await proxy(method, **params)
                else:
                    function = getattr(self.runtime, "fetch_bytes" if method == "fetch" else method)
                    result = function(**params)
                    if inspect.isawaitable(result):
                        result = await result
                if method == "fetch":
                    if not isinstance(result, bytes):
                        raise TypeError("Binary fetch did not return bytes")
                    if len(result) > self.max_response_bytes:
                        raise ValueError("Compute response exceeds the configured byte limit")
                    response = [{"binary": "pickle"}, result]
                else:
                    if method in {"handshake", "resume"}:
                        result = capabilities(result)
                    response = [{"result": result}]
                await self._reply(identity, request_id, response)
        except Exception as error:
            response = {"error": error_to_wire(error)}
            if isinstance(error, TimeoutError):
                response["retryable"] = True
            await self._reply(identity, request_id, [response])
        finally:
            self._counts[lane] -= 1

    async def _reply(self, identity, request_id, response):
        metadata = json.dumps(response[0], separators=(",", ":")).encode()
        if len(metadata) + sum(map(len, response[1:])) > self.max_response_bytes:
            metadata = b'{"error":{"code":"ResponseTooLarge","message":"Compute response exceeds the configured byte limit"}}'
            response = [None]
        try:
            async with asyncio.timeout(self.timeout):
                async with self._sending:
                    await self._socket.send_multipart([identity, request_id, metadata, *response[1:]],
                                                      flags=self._zmq.DONTWAIT)
        except (TimeoutError, self._zmq.ZMQError):
            # Lost reply does not revoke the session or undo an accepted call.
            pass

    async def close(self):
        pending = [task for task in (self._reader, *self._active) if task is not None]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._active.clear()
        self._reader = None
        if self._socket is not None:
            self._socket.close(0)
            self._socket = None
        if self._context is not None:
            self._context.destroy(linger=0)
            self._context = None
        for path, identity in self._ipc_files.items():
            try:
                observed = path.lstat()
                if stat.S_ISSOCK(observed.st_mode) and (observed.st_dev, observed.st_ino) == identity:
                    path.unlink()
            except FileNotFoundError:
                pass
        self._ipc_files.clear()
        self.endpoints.clear()
