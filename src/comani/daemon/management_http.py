"""Authenticated management RPC and bounded raw-byte runtime forwarding."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import secrets
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from multidict import CIMultiDict

from comani.files import FILE_CHUNK_SIZE, FILE_VERSION

from . import file_http
from .supervisor import SupervisorError

_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailer", "transfer-encoding", "upgrade", "host"}


def _headers(headers):
    removed = _HOP | {part.strip().lower() for part in headers.get("Connection", "").split(",")}
    return CIMultiDict((key, value) for key, value in headers.items()
                        if key.lower() not in removed)


class ManagedFiles:
    """Token-authorized file I/O independent of the compute session lifetime."""

    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.requests = {}

    def _session(self, session):
        if self.supervisor._closing:
            raise SupervisorError("DaemonClosing", "The management daemon is closing")
        if session == "raw":
            raise ValueError("The native prompt channel cannot use managed files")

    async def file_changed(self, root):
        await self.supervisor.files_changed(root.category)

    def _file_store(self):
        if self.supervisor.file_store is None:
            raise SupervisorError("RootsUnavailable", "Native file roots have not been discovered")
        return self.supervisor.file_store


def create_management_app(supervisor, *, token=None, service=None, auto_start=False,
                          management_limits=None):
    if service is not None and management_limits is not None:
        raise ValueError("Pass service or management_limits, not both")
    if service is None:
        from .management.service import ManagementService

        service = ManagementService(supervisor, operation_limits=management_limits)
    files = ManagedFiles(supervisor)
    identity = "configured-owner"
    management_session = secrets.token_urlsafe(24)
    startup_task = None

    @web.middleware
    async def authenticate(request, handler):
        if token:
            if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token):
                return web.json_response({"error": {"code": "Unauthorized", "message": "Invalid management token"}}, status=401)
        else:
            try:
                local = ipaddress.ip_address(request.remote).is_loopback
                hostname = urlsplit("//" + request.host).hostname
                local_host = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
            except (ValueError, TypeError):
                local = local_host = False
            if not local:
                return web.json_response({"error": {"code": "Unauthorized", "message": "Remote access requires a token"}}, status=401)
            origin = request.headers.get("Origin")
            if not local_host or (origin is not None and origin != f"{request.scheme}://{request.host}"):
                return web.json_response({"error": {"code": "ForbiddenOrigin", "message": "Loopback access requires a local Host and same-origin browser access"}}, status=403)
        try:
            if request.path.startswith("/files/"):
                await supervisor.get_file_store()
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as error:
            return web.json_response({"error": {
                "code": getattr(error, "code", type(error).__name__), "message": str(error),
                "details": getattr(error, "details", None)}}, status=400)

    app = web.Application(middlewares=[authenticate], client_max_size=128 * 1024**2)
    app["supervisor"] = supervisor
    app["management"] = service

    async def manage(request):
        body = await request.json()
        method, params = body.get("method"), body.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            raise ValueError("Expected management method and object params")
        if method == "handshake":
            window = await service.dispatch("management_window", {}, identity)
            result = {**window, "session_id": management_session,
                      "capabilities": {"management": 1, "files": FILE_VERSION, "files_resume": 1},
                      "file_transfer_epoch": app["file_transfers"].epoch,
                      "file_limits": supervisor.file_limits(),
                      "node_metadata": supervisor.observed_nodes()}
        elif method in {"file_roots", "file_stat", "file_remove"}:
            await supervisor.get_file_store()
            result = await getattr(file_http, method)(files, **params)
        else:
            result = await service.dispatch(method, params, identity)
        return web.json_response({"result": result})

    async def ready():
        if supervisor.state == "starting" and startup_task is not None and not startup_task.done():
            await asyncio.shield(startup_task)
        if supervisor.state != "running" or supervisor.endpoint is None:
            raise SupervisorError("RuntimeStopped", "The runtime is unavailable; call comfyui.start() explicitly")

    async def proxy(request):
        await ready()
        target = supervisor.process
        client = await supervisor._http()
        headers = _headers(request.headers)
        headers["Authorization"] = "Bearer " + supervisor._token
        url = supervisor.endpoint + request.rel_url.raw_path_qs
        try:
            async with client.request(request.method, url, headers=headers,
                                      data=request.content.iter_chunked(FILE_CHUNK_SIZE),
                                      allow_redirects=False) as upstream:
                response = web.StreamResponse(status=upstream.status, reason=upstream.reason,
                                              headers=_headers(upstream.headers))
                await response.prepare(request)
                try:
                    async for chunk in upstream.content.iter_chunked(FILE_CHUNK_SIZE):
                        await response.write(chunk)
                    await response.write_eof()
                except BaseException:
                    if request.transport:
                        request.transport.close()
                    raise
                return response
        except aiohttp.ClientError as error:
            if (target is not supervisor.process or target.returncode is not None
                    or supervisor.state in {"stopping", "stopped", "starting", "failed"}):
                raise SupervisorError("RefExpired", "The runtime lifecycle ended; old computation calls and references expired") from error
            raise SupervisorError("RuntimeUnavailable", "The native runtime connection ended") from error

    async def websocket(request):
        await ready()
        client = await supervisor._http()
        outgoing = web.WebSocketResponse(heartbeat=20)
        async with client.ws_connect(supervisor.endpoint + request.rel_url.raw_path_qs,
                                     heartbeat=20, max_msg_size=0) as upstream:
            await outgoing.prepare(request)

            async def forward(source, target):
                async for message in source:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        await target.send_str(message.data)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        await target.send_bytes(message.data)
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        break

            tasks = [asyncio.create_task(forward(upstream, outgoing)),
                     asyncio.create_task(forward(outgoing, upstream))]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await outgoing.close()
        return outgoing

    async def startup(app):
        nonlocal startup_task
        if auto_start:
            async def launch():
                try:
                    await supervisor.start()
                except Exception:
                    # Startup failure belongs to runtime state. The management
                    # connection remains available to inspect and repair it.
                    pass
            startup_task = asyncio.create_task(launch())

    async def cleanup(app):
        if startup_task and not startup_task.done():
            startup_task.cancel()
            await asyncio.gather(startup_task, return_exceptions=True)
        await service.close()
        await supervisor.close()

    app.router.add_post("/manage", manage)
    install_file_routes = file_http.install_file_routes
    install_file_routes(app, files)
    app.router.add_get("/ws", websocket)
    app.router.add_route("*", "/{path:.*}", proxy)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app
