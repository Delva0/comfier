"""Real native/HTTP reproduction of pending-call pool control starvation."""
import argparse
import asyncio
import json
import socket
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from independent_load import Echo, Slow

from comfier import Client
from comfier.daemon import NativeRuntime
from comfier.daemon.http import create_app


async def main(args):
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", args.port))
    runtime = await NativeRuntime("/home/delva/projects/ComfyUI", cpu=True,
                                  custom_nodes=False, session_ttl=30).start()
    runtime.adapter.nodes.NODE_CLASS_MAPPINGS.update(IndependentSlow=Slow, IndependentEcho=Echo)
    runner = web.AppRunner(create_app(runtime))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", args.port).start()
    url = f"http://127.0.0.1:{args.port}"
    client = await Client(url).start()
    try:
        slow = client.node("IndependentSlow", return_call=True, value=42, delay=3.0)
        calls = [slow] + [client.node("IndependentEcho", return_call=True, value=slow.output()) for _ in range(127)]
        await asyncio.sleep(.8)
        count = sum(s == client.session_id for s, _ in runtime.calls)
        first_done_before_probe = runtime.calls[(client.session_id, slow.id)].done.done()
        acquired = len(client.transport._session.connector._acquired)
        wait_session = getattr(client.transport, "_wait_session", None)
        pending_connections = None if wait_session is None else len(wait_session.connector._acquired)
        start = time.perf_counter()
        async with aiohttp.ClientSession() as separate:
            async with separate.get(url + "/queue") as response:
                assert response.status == 200
                await response.json()
        separate_seconds = time.perf_counter() - start
        start = time.perf_counter()
        heartbeat_timeout = False
        try:
            await asyncio.wait_for(client.transport.request("heartbeat", session_id=client.session_id), .3)
        except asyncio.TimeoutError:
            heartbeat_timeout = True
        heartbeat_seconds = time.perf_counter() - start
        start = time.perf_counter()
        close_timeout = False
        try:
            await asyncio.wait_for(client.close(wait=False), .3)
        except asyncio.TimeoutError:
            close_timeout = True
        close_seconds = time.perf_counter() - start
        await asyncio.wait_for(runtime.queue.join(), 5)
        await asyncio.sleep(.05)
        transport_sessions_closed = all(getattr(client.transport, name, None) is None
                                        for name in ("_session", "_wait_session", "_data_session"))
        result = {"calls_created": len(calls), "accepted_after_800ms": count,
                  "native_first_done_before_control_test": first_done_before_probe,
                  "http_connections_acquired_before_probe": acquired,
                  "separate_wait_connections_before_probe": pending_connections,
                  "separate_control_ms": round(separate_seconds * 1000, 3),
                  "sdk_heartbeat_timed_out_300ms": heartbeat_timeout,
                  "sdk_heartbeat_ms": round(heartbeat_seconds * 1000, 3),
                  "sdk_close_wait_false_timed_out_300ms": close_timeout,
                  "sdk_close_ms": round(close_seconds * 1000, 3),
                  "all_http_sessions_closed": transport_sessions_closed,
                  "sdk_watchers_after_close": len(client._watchers),
                  "tasks_after_native_settle": len(asyncio.all_tasks()),
                  "runtime_calls_after_settle": len(runtime.calls),
                  "runtime_objects_after_settle": len(runtime.store.entries),
                  "passed": count == 128 and not heartbeat_timeout and not close_timeout
                            and transport_sessions_closed and not client._watchers
                            and not runtime.calls and not runtime.store.entries}
        args.output.write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)
    finally:
        await client.close(wait=False)
        await runtime.close_session(client.session_id)
        await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8190)
    parser.add_argument("--output", type=Path, default=Path("tests/stress/connection_pool_results.json"))
    asyncio.run(main(parser.parse_args()))
