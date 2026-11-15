"""Transport boundaries, also usable against an in-process runtime in tests."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import threading
import weakref
from concurrent.futures import Future
from typing import Any

import aiohttp

from ._codec import decompress_pickle, has_lz4
from .errors import RemoteError, SessionClosed, error_from_wire, error_to_wire
from .protocol import (
    BINARY_FETCH_LZ4_VERSION,
    BINARY_FETCH_VERSION,
    LZ4_ACCEPT_HEADER,
    LZ4_CODEC,
    LZ4_PICKLE_CONTENT_TYPE,
    PICKLE_CONTENT_TYPE,
    PICKLE_LENGTH_HEADER,
)


def _retryable_heartbeat_status(status):
    return status in {408, 429} or 500 <= status < 600


async def _post(session, url, method, params, *, retry_http=False, binary=False, lz4=False):
    options = {"json": {"method": method, "params": params}}
    if binary:
        options["headers"] = {"Accept": PICKLE_CONTENT_TYPE}
        if lz4:
            options["headers"][LZ4_ACCEPT_HEADER] = LZ4_CODEC
    async with session.post(url, **options) as response:
        if binary and response.content_type in {PICKLE_CONTENT_TYPE, LZ4_PICKLE_CONTENT_TYPE}:
            response.raise_for_status()
            compressed = response.content_type == LZ4_PICKLE_CONTENT_TYPE
            if compressed and not lz4:
                raise RemoteError("Daemon returned LZ4 without codec negotiation")
            payload = await response.read()
            return (decompress_pickle(payload, response.headers.get(PICKLE_LENGTH_HEADER))
                    if compressed else payload)
        try:
            payload = await response.json()
        except (aiohttp.ContentTypeError, ValueError):
            response.raise_for_status()
            raise RemoteError("Daemon returned an invalid JSON response") from None
        if retry_http and _retryable_heartbeat_status(response.status):
            error = payload.get("error") if isinstance(payload, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            if code not in {"SessionClosed", "Unauthorized", "ForbiddenOrigin"}:
                response.raise_for_status()
        if retry_http and not isinstance(payload, dict):
            raise RemoteError("Daemon returned a non-object RPC response", details={"status": response.status})
        if "error" in payload:
            raise error_from_wire(payload["error"])
        if payload.get("ok") is False:
            raise error_from_wire(payload.get("error", {"message": str(payload)}))
        response.raise_for_status()
    return payload.get("result", payload)


class _HTTPHeartbeat:
    """One lease, with connections and an event loop owned by its own thread."""

    def __init__(self, url, headers, session_id, interval, owner_loop, notify):
        self.url, self.headers, self.session_id = url, dict(headers), session_id
        self.interval = interval
        self.ready, self.done = Future(), Future()
        self.error = None
        self.terminal = False
        self._owner_loop = weakref.ref(owner_loop)
        self._notify = notify  # Must not retain the Client.
        self._stopped = threading.Event()
        self._lock = threading.Lock()
        self._loop = self._task = None
        self._notice_pending = False
        self._thread = contextvars.Context().run(
            threading.Thread, target=self._run, daemon=True,
            name=f"comani-heartbeat-{session_id[:8]}")
        contextvars.Context().run(self._thread.start)

    def _notice(self):
        loop = self._owner_loop()
        if loop is None or loop.is_closed():
            self.stop()
            return
        with self._lock:
            if self._notice_pending:
                return
            self._notice_pending = True
        try:
            loop.call_soon_threadsafe(self._deliver, context=contextvars.Context())
        except RuntimeError:
            self.stop()

    def _deliver(self):
        with self._lock:
            self._notice_pending = False
        self._notify(self.error, self.terminal)

    def _run(self):
        try:
            asyncio.run(self._serve())
        except BaseException as error:
            if not self._stopped.is_set():
                self.error = SessionClosed(f"Session heartbeat failed: {error}")
                self.terminal = True
                self._notice()
            if not self.ready.done():
                self.ready.set_exception(SessionClosed("Session heartbeat could not start"))
        finally:
            if not self.ready.done():
                self.ready.set_result(None)
            self.done.set_result(None)

    async def _watch_owner(self):
        while True:
            await asyncio.sleep(0.1)
            loop = self._owner_loop()
            if loop is None or loop.is_closed():
                self.stop()
                return

    async def _serve(self):
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
        if self._stopped.is_set():
            return
        watcher = asyncio.create_task(self._watch_owner(), context=contextvars.Context())
        try:
            async with aiohttp.ClientSession(headers=self.headers,
                    timeout=aiohttp.ClientTimeout(total=self.interval),
                    connector=aiohttp.TCPConnector(limit=1)) as session:
                if not self.ready.done():
                    self.ready.set_result(None)
                while not self._stopped.is_set():
                    try:
                        await _post(session, self.url, "heartbeat", {"session_id": self.session_id},
                                    retry_http=True)
                        self.error = None
                    except (aiohttp.ClientConnectionError, aiohttp.ClientPayloadError, TimeoutError, OSError) as error:
                        # The server owns lease expiry, including active-request
                        # exemptions. Retry this ID; never handshake or resubmit.
                        self.error = RemoteError(f"Session heartbeat unavailable ({type(error).__name__}): {error}")
                    except aiohttp.ClientResponseError as error:
                        terminal = not _retryable_heartbeat_status(error.status)
                        failure = SessionClosed if terminal else RemoteError
                        self.error = failure(f"Session heartbeat HTTP {error.status}: {error.message}",
                                             details={"status": error.status})
                        self.terminal = terminal
                    except Exception as error:
                        self.error = SessionClosed(f"Session heartbeat failed: {error}",
                                                   details=getattr(error, "details", None))
                        self.terminal = True
                    self._notice()
                    if self.terminal:
                        return
                    await asyncio.sleep(self.interval)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    def stop(self):
        self._stopped.set()
        with self._lock:
            loop, task = self._loop, self._task
        if loop is not None and not loop.is_closed() and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel, context=contextvars.Context())
            except RuntimeError:
                pass

    async def close(self):
        self.stop()
        await asyncio.shield(asyncio.wrap_future(self.done))
        self._thread.join()


class HTTPTransport:
    def __init__(self, url: str, *, timeout: float = 3600, token: str | None = None,
                 fetch_compression: str = "auto"):
        if fetch_compression not in ("auto", "identity"):
            raise ValueError("fetch_compression must be 'auto' or 'identity'")
        self._fetch_compression = fetch_compression
        self.url = url.rstrip("/") + "/rpc"
        self.timeout = timeout
        self._headers = {"Authorization": "Bearer " + token} if token else {}
        self._session = None
        self._wait_session = None
        self._data_session = None
        self._binary_fetch = False
        self._binary_fetch_lz4 = False

    async def open(self):
        if self._session is None:
            def session(limit):
                return aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout), headers=self._headers,
                    connector=aiohttp.TCPConnector(limit=limit))

            self._session = session(32)
            self._wait_session = session(100)
            self._data_session = session(4)

    async def request(self, method: str, **params) -> Any:
        if self._session is None:
            raise SessionClosed("Transport is not open")
        # Result long polls and bulk transfers must not consume the connections
        # needed to admit calls, renew leases, release objects, or close a session.
        session = self._session
        if method == "wait_result":
            session = self._wait_session
        elif method in {"fetch", "artifact_put", "file_stat"}:
            session = self._data_session
        result = await _post(session, self.url, method, params,
                             binary=method == "fetch" and self._binary_fetch,
                             lz4=method == "fetch" and self._binary_fetch_lz4)
        if method == "handshake":
            capabilities = result.get("capabilities", {})
            version = capabilities.get("binary_fetch") if isinstance(capabilities, dict) else None
            self._binary_fetch = type(version) is int and version == BINARY_FETCH_VERSION
            version = capabilities.get("binary_fetch_lz4") if isinstance(capabilities, dict) else None
            self._binary_fetch_lz4 = (self._binary_fetch and type(version) is int
                                     and version == BINARY_FETCH_LZ4_VERSION and has_lz4()
                                     and self._fetch_compression == "auto")
        return result

    def _start_heartbeat(self, session_id, interval, owner_loop, notify):
        return _HTTPHeartbeat(self.url, self._headers, session_id, interval, owner_loop, notify)

    def _file_connection(self):
        if self._data_session is None:
            raise SessionClosed("Transport is not open")
        return self._data_session, self.url[:-4]

    async def close(self):
        for attribute in ("_session", "_wait_session", "_data_session"):
            session = getattr(self, attribute)
            if session is not None:
                await session.close()
                setattr(self, attribute, None)


class InProcessTransport:
    def __init__(self, runtime):
        self.runtime = runtime

    async def open(self):
        pass

    async def request(self, method: str, **params):
        function = getattr(self.runtime, method, None)
        if function is None:
            raise RemoteError(f"Runtime does not implement {method!r}")
        failure = None
        try:
            result = function(**params)
            return await result if inspect.isawaitable(result) else result
        except Exception as error:
            failure = error_from_wire(error_to_wire(error))
            # A runtime Future may retain this original exception. Do not let its
            # traceback capture this request frame and the client's new error.
            error.__traceback__ = None
        # Cross the same error boundary as HTTP without attaching native frames.
        raise failure from None

    async def close(self):
        pass
