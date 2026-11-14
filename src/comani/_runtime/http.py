from __future__ import annotations

import asyncio
import hmac
import ipaddress
import uuid
from urllib.parse import urlsplit

from aiohttp import web

from .objects import SessionClosed


def create_app(runtime, *, token=None, close_runtime=True):
    methods = {"handshake", "submit", "wait_result", "fetch", "release", "forget_calls", "close_session", "artifact_put", "heartbeat"}

    @web.middleware
    async def authenticate(request, handler):
        if token and not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + token):
            return web.json_response({"error": {"code": "Unauthorized", "message": "Invalid runtime token"}}, status=401)
        if not token:
            try:
                local = ipaddress.ip_address(request.remote).is_loopback
            except (ValueError, TypeError):
                local = False
            if not local:
                return web.json_response({"error": {"code": "Unauthorized", "message": "An authentication token is required for remote access"}}, status=401)
            try:
                hostname = urlsplit("//" + request.host).hostname
                local_host = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
            except (ValueError, TypeError):
                local_host = False
            origin = request.headers.get("Origin")
            if not local_host or (origin is not None and origin != f"{request.scheme}://{request.host}"):
                return web.json_response({"error": {"code": "ForbiddenOrigin", "message": "Unauthenticated loopback requests require a local Host and same-origin browser access"}}, status=403)
        try:
            return await handler(request)
        except Exception as error:
            return web.json_response({"error": {"code": getattr(error, "code", type(error).__name__), "message": str(error)}}, status=400)

    app = web.Application(middlewares=[authenticate], client_max_size=128 * 1024**2)
    app["runtime"] = runtime

    async def rpc(request):
        body = await request.json()
        method = body.get("method")
        if method not in methods:
            raise ValueError("Unknown RPC method")
        params = body.get("params", {})
        session = params.get("session_id")
        if session == "raw":
            raise SessionClosed("The native prompt channel is reserved; open a client session")
        transport = request.transport
        tracked = session and transport and session in runtime.session_seen
        if tracked:
            runtime.requests.setdefault(session, set()).add(transport)
        try:
            result = await getattr(runtime, method)(**params)
        finally:
            if tracked:
                requests = runtime.requests.get(session)
                if requests is not None:
                    requests.discard(transport)
                    if not requests:
                        runtime.requests.pop(session, None)
        return web.json_response({"result": result})

    async def prompt(request):
        body = await request.json()
        call_id = body.get("prompt_id") or str(uuid.uuid4())
        await runtime.start()
        body = runtime.adapter.server.trigger_on_prompt(body)
        runtime.adapter.server.node_replace_manager.apply_replacements(body["prompt"])
        valid, error, targets, node_errors = await runtime.adapter.execution.validate_prompt(call_id, body["prompt"], body.get("partial_execution_targets"))
        if not valid:
            return web.json_response({"error": error, "node_errors": node_errors}, status=400)
        spec = {"kind": "workflow", "graph": {"nodes": body["prompt"], "native": True, "targets": targets, "outputs": []}, "extra_data": body.get("extra_data", {})}
        runtime._open_raw_session()
        await runtime.submit("raw", call_id, spec)
        return web.json_response({"prompt_id": call_id, "number": len(runtime.calls), "node_errors": {}})

    async def history(request):
        result = {}
        for (session, call_id), state in runtime.calls.items():
            if session != "raw" or (request.match_info.get("prompt_id") and request.match_info["prompt_id"] != call_id):
                continue
            if state.status in ("succeeded", "failed", "cancelled"):
                native = state.done.result().get("native", {}) if state.status == "succeeded" else {}
                result[call_id] = {"outputs": native.get("history", {}).get("outputs", {}), "status": {"status_str": "success" if state.status == "succeeded" else "error", "completed": state.status == "succeeded"}}
        return web.json_response(result)

    async def queue(request):
        running, pending = [], []
        for state in runtime.calls.values():
            if state.status == "running":
                running.append(state.call_id)
            elif state.status in ("accepted", "queued"):
                pending.append(state.call_id)
        return web.json_response({"queue_running": running, "queue_pending": pending})

    async def websocket(request):
        socket = web.WebSocketResponse(heartbeat=20)
        await socket.prepare(request)
        events = asyncio.Queue()
        runtime.listeners.add(events)
        task = receive = None
        try:
            while not socket.closed:
                task = asyncio.create_task(events.get())
                receive = asyncio.create_task(socket.receive())
                done, _ = await asyncio.wait((task, receive), return_when=asyncio.FIRST_COMPLETED)
                if receive in done:
                    task.cancel()
                    break
                receive.cancel()
                await socket.send_json(task.result())
        finally:
            runtime.listeners.discard(events)
            children = [child for child in (task, receive) if child is not None]
            for child in children:
                child.cancel()
            await asyncio.gather(*children, return_exceptions=True)
        return socket

    async def startup(app):
        await runtime.start()

    async def cleanup(app):
        if close_runtime:
            await runtime.close()

    app.router.add_post("/rpc", rpc)
    app.router.add_post("/prompt", prompt)
    app.router.add_get("/history", history)
    app.router.add_get("/history/{prompt_id}", history)
    app.router.add_get("/queue", queue)
    app.router.add_get("/ws", websocket)
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    return app
