"""Optional socket transport; importing the SDK does not import pyzmq."""

from __future__ import annotations

import asyncio
import json
import uuid
import weakref
from contextlib import asynccontextmanager

from ._zmq_protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    VERSION,
    curve_key,
    dependency,
    limit,
    positive,
)
from ._zmq_protocol import (
    endpoint as validate_endpoint,
)
from .errors import RemoteError, SessionClosed, error_from_wire


class ZMQTransport:
    """Compute over IPC or TCP, with optional HTTP management/file companion.

    A request deadline includes credit admission, send and response receipt.
    Socket reconnection preserves no objects itself: Client performs same-session
    resume and reuses the original submission identities after uncertain replies.
    """

    def __init__(self, endpoint, *, token, timeout=60, max_inflight=16,
                 max_request_bytes=MAX_REQUEST_BYTES, max_response_bytes=MAX_RESPONSE_BYTES,
                 curve_server_key=None, management_url=None):
        if not isinstance(token, str) or not token:
            raise ValueError("ZMQ compute requires a nonempty authentication token")
        curve_server_key = curve_key(curve_server_key, "curve_server_key")
        self.endpoint = validate_endpoint(endpoint, encrypted=bool(curve_server_key))
        self.timeout = positive(timeout, "timeout")
        self.max_inflight = limit(max_inflight, "max_inflight")
        self.max_request_bytes = limit(max_request_bytes, "max_request_bytes")
        self.max_response_bytes = limit(max_response_bytes, "max_response_bytes")
        self._token, self.curve_server_key = token, curve_server_key
        self._context = self._socket = self._reader = None
        self._pending = {}
        self._credit = asyncio.Semaphore(self.max_inflight)
        self._lanes = {"control": asyncio.Semaphore(self.max_inflight),
                       "wait": asyncio.Semaphore(max(1, self.max_inflight // 2)),
                       "data": asyncio.Semaphore(max(1, self.max_inflight // 4))}
        self._sending = asyncio.Lock()
        self._closed = False
        self._http = None
        if management_url is not None:
            from .transport import HTTPTransport
            self._http = HTTPTransport(management_url, token=token, timeout=timeout)

    @property
    def supports_management(self):
        return self._http is not None

    async def open(self):
        if self._closed:
            raise SessionClosed("Transport is closed")
        if self._socket is None:
            zmq = dependency()
            if self._context is None:
                self._context = zmq.asyncio.Context()
            socket = self._context.socket(zmq.DEALER)
            for option, value in ((zmq.LINGER, 0), (zmq.SNDHWM, self.max_inflight),
                                  (zmq.RCVHWM, self.max_inflight), (zmq.IMMEDIATE, 1),
                                  (zmq.MAXMSGSIZE, self.max_response_bytes)):
                socket.setsockopt(option, value)
            try:
                if self.curve_server_key:
                    socket.curve_publickey, socket.curve_secretkey = zmq.curve_keypair()
                    socket.curve_serverkey = self.curve_server_key
                socket.connect(self.endpoint)
            except BaseException:
                socket.close(0)
                raise
            self._socket = socket
            self._reader = asyncio.create_task(self._receive(socket))
        if self._http is not None:
            await self._http.open()

    async def _receive(self, socket):
        try:
            while True:
                try:
                    async with asyncio.timeout(self.timeout):
                        frames = await socket.recv_multipart()
                except TimeoutError:
                    continue
                try:
                    if len(frames) not in {2, 3}:
                        raise ConnectionError("Invalid compute response frame count")
                    request_id, metadata = frames[:2]
                    future = self._pending.get(request_id)
                    if future is None or future.done():
                        continue
                    if sum(map(len, frames[1:])) > self.max_response_bytes:
                        raise ConnectionError("Compute response exceeds the configured byte limit")
                    payload = json.loads(metadata)
                    if not isinstance(payload, dict):
                        raise ConnectionError("Expected a compute response object")
                    if "error" in payload:
                        error = (ConnectionError(payload["error"].get("message", "Compute request unavailable")) if payload.get("retryable")
                                 else error_from_wire(payload["error"]))
                        future.set_exception(error)
                    elif payload.get("binary") == "pickle" and len(frames) == 3:
                        future.set_result(frames[2])
                    elif "result" in payload and len(frames) == 2:
                        future.set_result(payload["result"])
                    else:
                        raise ConnectionError("Invalid compute response envelope")
                finally:
                    # Completed errors gain caller tracebacks, and binary
                    # frames may be large. An idle reader must retain neither.
                    frames = request_id = metadata = future = payload = error = None
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail_pending("Compute connection failed")
            # This socket is no longer reusable. A following same-session resume
            # opens another socket; it never creates a new runtime session.
            if self._socket is socket:
                self._socket = self._reader = None
            socket.close(0)

    def _fail_pending(self, message):
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(ConnectionError(message))

    async def reconnect(self, endpoint=None):
        if endpoint is not None:
            self.endpoint = validate_endpoint(endpoint, encrypted=bool(self.curve_server_key))
        socket, reader = self._socket, self._reader
        self._socket = self._reader = None
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        if socket is not None:
            socket.close(0)
        self._fail_pending("Compute connection reset")

    async def request(self, method, **params):
        if method.startswith("management:"):
            return await self.management_request(method.partition(":")[2], **params)
        if self._http is not None and method in {"file_roots", "file_stat", "file_remove"}:
            return await self.management_request(method, **params)
        encoded = json.dumps({"version": VERSION, "token": self._token,
                              "method": method, "params": params,
                              "timeout": self.timeout}, separators=(",", ":")).encode()
        if len(encoded) > self.max_request_bytes:
            raise ValueError("Compute request exceeds the configured byte limit")
        future = request_id = None
        lane = "wait" if method == "wait_result" else "data" if method in {"fetch", "artifact_put"} else "control"
        try:
            async with asyncio.timeout(self.timeout):
                async with self._lanes[lane], self._credit:
                    await self.open()
                    socket = self._socket
                    request_id = uuid.uuid4().hex.encode()
                    future = asyncio.get_running_loop().create_future()
                    self._pending[request_id] = future
                    async with self._sending:
                        if socket is not self._socket:
                            if self._closed:
                                raise SessionClosed("Transport is closed")
                            raise ConnectionError("Compute connection reset")
                        await socket.send_multipart([request_id, encoded])
                    return await future
        except TimeoutError:
            await self.reconnect()
            raise TimeoutError("Compute request deadline exceeded") from None
        except asyncio.CancelledError:
            # pyzmq also cancels send futures when another request resets the
            # socket. Only an actual Task cancellation belongs to the caller.
            if asyncio.current_task().cancelling():
                raise
            if self._closed:
                raise SessionClosed("Transport is closed") from None
            raise ConnectionError("Compute connection reset") from None
        except Exception as error:
            zmq = dependency()
            if isinstance(error, zmq.ZMQError):
                await self.reconnect()
                raise ConnectionError("Compute connection failed") from None
            raise
        finally:
            if request_id is not None:
                self._pending.pop(request_id, None)
            if future is not None and not future.done():
                future.cancel()
            elif future is not None and not future.cancelled():
                # A send failure can race a reader failure before awaiting it.
                future.exception()

    async def management_request(self, method, **params):
        if self._http is None:
            raise RemoteError("This transport has no HTTP management companion",
                              details={"code": "UnsupportedTransport"})
        return await self._http.management_request(method, **params)

    async def native_request(self, method, path, *, params=None, json=None):
        if self._http is None:
            raise RemoteError("Native control requests require an HTTP management companion",
                              details={"code": "UnsupportedTransport"})
        return await self._http.native_request(method, path, params=params, json=json)

    def _file_connection(self):
        if self._http is None:
            raise RemoteError("File streaming requires an HTTP management companion",
                              details={"code": "UnsupportedTransport"})
        return self._http._file_connection()

    def _start_heartbeat(self, session_id, interval, owner_loop, notify):
        from .transport import _HTTPHeartbeat

        owner = weakref.ref(self)
        options = {"token": self._token, "timeout": min(self.timeout, interval),
                   "curve_server_key": self.curve_server_key,
                   "max_request_bytes": self.max_request_bytes,
                   "max_response_bytes": self.max_response_bytes}

        class Keeper(_HTTPHeartbeat):
            @asynccontextmanager
            async def _connection(keeper):
                source = owner()
                if source is None:
                    raise SessionClosed("Transport is gone")
                transport = ZMQTransport(source.endpoint, **options)
                del source
                try:
                    await transport.open()
                    yield transport
                finally:
                    await transport.close()

            async def _renew(keeper, transport):
                source = owner()
                if source is None:
                    raise SessionClosed("Transport is gone")
                destination = source.endpoint
                del source
                if destination != transport.endpoint:
                    await transport.reconnect(destination)
                await transport.request("heartbeat", session_id=session_id)

        return Keeper(None, {}, session_id, interval, owner_loop, notify)

    async def close(self):
        self._closed = True
        await self.reconnect()
        if self._context is not None:
            self._context.destroy(linger=0)
            self._context = None
        if self._http is not None:
            await self._http.close()
