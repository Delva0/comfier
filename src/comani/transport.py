"""Transport boundaries, also usable against an in-process runtime in tests."""

from __future__ import annotations

import inspect
from typing import Any

import aiohttp

from .errors import RemoteError, SessionClosed, error_from_wire, error_to_wire


class HTTPTransport:
    def __init__(self, url: str, *, timeout: float = 3600, token: str | None = None):
        self.url = url.rstrip("/") + "/rpc"
        self.timeout = timeout
        self._headers = {"Authorization": "Bearer " + token} if token else {}
        self._session = None
        self._wait_session = None
        self._data_session = None

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
        elif method in {"fetch", "artifact_put"}:
            session = self._data_session
        async with session.post(self.url, json={"method": method, "params": params}) as response:
            try:
                payload = await response.json()
            except (aiohttp.ContentTypeError, ValueError):
                response.raise_for_status()
                raise RemoteError("Daemon returned an invalid JSON response") from None
            if "error" in payload:
                raise error_from_wire(payload["error"])
            if payload.get("ok") is False:
                raise error_from_wire(payload.get("error", {"message": str(payload)}))
            response.raise_for_status()
        return payload.get("result", payload)

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
