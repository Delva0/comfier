"""Discover a verified compute channel while retaining the HTTP control plane."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from ._zmq_transport import ZMQTransport
from .errors import RemoteError
from .transport import HTTPTransport, retryable_transport_error


def _private_socket(endpoint):
    """Filesystem visibility alone does not establish daemon identity."""
    if os.name != "posix":
        return False
    try:
        path = Path(endpoint[6:])
        socket, parent = path.lstat(), path.parent.lstat()
        return (path.is_absolute() and stat.S_ISSOCK(socket.st_mode)
                and socket.st_uid == parent.st_uid == os.getuid()
                and stat.S_ISDIR(parent.st_mode) and not parent.st_mode & 0o077)
    except OSError:
        return False


class AutoTransport(HTTPTransport):
    """One stable owner for discovery, compute, files and independent leases.

    HTTP creates the session. Candidate probes create no sessions and must prove
    both the advertised gateway identity and the canonical runtime epoch. A lost
    compute response changes the route, then lets Client resume the original
    session before retrying its original request identity.
    """

    def __init__(self, url, *, token=None, **options):
        super().__init__(url, token=token, **options)
        self._token = token
        self._discovery = {}
        self._compute = None
        self._retired = []
        self._http_fetch = False
        self.selection_reason = "http_bootstrap"

    @property
    def selected_transport(self):
        return "zmq" if self._compute is not None else "http"

    @property
    def compute_endpoint(self):
        return self._compute.endpoint if self._compute is not None else None

    async def _select_compute(self, epoch):
        discovery = self._discovery
        token = self._token or discovery.get("compute_token")
        capabilities = discovery.get("capabilities", {})
        candidates = discovery.get("compute_endpoints", ())
        if (not isinstance(capabilities, dict) or capabilities.get("compute_zmq") != 1
                or not isinstance(token, str) or not token or not isinstance(candidates, list)):
            self.selection_reason = discovery.get("compute_fallback") or "http_only_daemon"
            return
        candidates = [item for item in candidates if isinstance(item, dict)]
        candidates.sort(key=lambda item: not str(item.get("endpoint", "")).startswith("ipc://"))
        self.selection_reason = "no_verified_compute_endpoint"
        for item in candidates[:4]:
            endpoint, server_id = item.get("endpoint"), item.get("server_id")
            if (item.get("protocol") != "zmq-v1" or not isinstance(endpoint, str)
                    or not isinstance(server_id, str) or not server_id):
                continue
            local = endpoint.startswith("ipc://")
            if local:
                if not _private_socket(endpoint):
                    continue
            elif not endpoint.startswith("tcp://") or not item.get("curve_server_key"):
                continue
            candidate = None
            try:
                candidate = ZMQTransport(endpoint, token=token, timeout=.3 if local else 1,
                    curve_server_key=item.get("curve_server_key"), expected_server_id=server_id)
                probe = await candidate.request("runtime_info")
                if not isinstance(probe, dict) or not epoch or probe.get("epoch") != epoch:
                    self.selection_reason = "compute_epoch_mismatch"
                    continue
                candidate.timeout = min(self.timeout, 60)
                self._compute, candidate = candidate, None
                self.selection_reason = "verified_local_ipc" if local else "verified_curve_tcp"
                return
            except (ImportError, ValueError, RemoteError, OSError, TimeoutError):
                # Discovery is optional. No computation or session was admitted
                # on a candidate, so keeping the canonical HTTP route is safe.
                pass
            finally:
                if candidate is not None:
                    await candidate.close()

    async def request(self, method, **params):
        if method.startswith("management:") or method in {"file_roots", "file_stat", "file_remove"}:
            result = await super().request(method, **params)
            if method == "management:handshake":
                self._discovery = result
            return result
        if method == "handshake":
            result = await super().request(method, **params)
            await self._select_compute(result.get("epoch"))
            return result
        compute = self._compute
        # Retain negotiated HTTP compression on remote bulk results. Local IPC
        # avoids network serialization overhead without an extra codec pass.
        if (compute is None or (method == "fetch" and (self._http_fetch or (self._binary_fetch_lz4
                                and compute.endpoint.startswith("tcp://"))))):
            return await super().request(method, **params)
        try:
            return await compute.request(method, **params)
        except Exception as error:
            if (method == "fetch" and isinstance(error, RemoteError)
                    and (error.details or {}).get("code") == "ResponseTooLarge"):
                # A fetch is a read. Keep large results on the existing HTTP
                # stream instead of imposing the optional socket frame limit.
                self._http_fetch = True
                return await super().request(method, **params)
            if retryable_transport_error(error) and compute is self._compute:
                self._compute = None
                self._retired.append(compute)
                self.selection_reason = "compute_connection_lost"
                # Inflight requests still own this transport. Closing it here
                # would turn their retryable disconnect into SessionClosed.
            raise

    async def reset_compute(self):
        transports = [*self._retired, *([self._compute] if self._compute is not None else [])]
        self._compute, self._retired = None, []
        self._http_fetch = False
        self.selection_reason = "http_bootstrap"
        self._reset_runtime_capabilities()
        for transport in transports:
            await transport.close()

    async def close(self):
        await self.reset_compute()
        await super().close()
