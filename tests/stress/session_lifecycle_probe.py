"""Independent stale-ID, fresh-handshake and inflight-close native HTTP checks."""
import argparse
import asyncio
import json
import socket
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from independent_load import Echo, Slow, rss_mib

from comani import Client
from comani.daemon import NativeRuntime
from comani.daemon.http import create_app


async def main(args):
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", args.port))
    runtime = await NativeRuntime("/home/delva/projects/ComfyUI", cpu=True,
                                  custom_nodes=False, session_ttl=.3).start()
    runtime.adapter.nodes.NODE_CLASS_MAPPINGS.update(IndependentEcho=Echo, IndependentSlow=Slow)
    runner = web.AppRunner(create_app(runtime))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", args.port).start()
    url = f"http://127.0.0.1:{args.port}"
    report = {"ttl_seconds": runtime.session_ttl, "checks": []}
    started = time.perf_counter()
    try:
        async with Client(url) as original:
            call = original.node("IndependentEcho", value=42)
            ref = await call.output()
            assert await ref.fetch() == 42
            old = {"session_id": original.session_id, "ref_id": ref.id,
                   "epoch": original.epoch, "call_id": call.id}
        async with aiohttp.ClientSession() as http:
            async def rpc(method, **params):
                async with http.post(url + "/rpc", json={"method": method, "params": params}) as response:
                    return response.status, await response.json()

            async def churn(count):
                for _ in range(count // 10):
                    opened = await asyncio.gather(*(rpc("handshake") for _ in range(10)))
                    ids = [payload["result"]["session_id"] for status, payload in opened if status == 200]
                    assert len(ids) == 10
                    closed = await asyncio.gather(*(rpc("close_session", session_id=sid) for sid in ids))
                    assert all(status == 200 for status, _ in closed)

            await churn(80)
            await asyncio.sleep(runtime.session_ttl * 3)
            assert old["session_id"] not in runtime.closed_sessions
            assert old["session_id"] not in runtime.session_seen
            report["checks"].append("old identifier absent from both active sessions and bounded diagnostics")
            tests = [
                ("heartbeat", {"session_id": old["session_id"]}),
                ("submit", {"session_id": old["session_id"], "call_id": "stale-resubmit", "spec": {
                    "kind": "node", "node_type": "IndependentEcho", "inputs": {"value": 99}}}),
                ("fetch", {key: old[key] for key in ("session_id", "ref_id", "epoch")}),
                ("wait_result", {key: old[key] for key in ("session_id", "call_id")}),
            ]
            outcomes = []
            for method, params in tests:
                status, payload = await rpc(method, **params)
                assert status == 400 and payload["error"]["code"] == "SessionClosed", (method, status, payload)
                assert old["session_id"] not in runtime.session_seen
                outcomes.append({"method": method, "status": status, "error": payload["error"]["code"]})
            report["stale_rpc_results"] = outcomes
            for method, params in [("close_session", {"session_id": old["session_id"]}),
                                   ("release", {"session_id": old["session_id"], "refs": [old["ref_id"]], "epoch": old["epoch"]})]:
                status, _ = await rpc(method, **params)
                assert status == 200
                assert old["session_id"] not in runtime.session_seen
            report["checks"].append("evicted stale ID cannot heartbeat/submit/fetch/wait; idempotent close/release does not recreate it")
            status, payload = await rpc("handshake", session_id=old["session_id"])
            assert status == 200
            new = payload["result"]["session_id"]
            assert new != old["session_id"] and old["session_id"] not in runtime.session_seen
            status, payload = await rpc("fetch", session_id=new, ref_id=old["ref_id"], epoch=old["epoch"])
            assert status == 400 and payload["error"]["code"] == "Expired", payload
            status, payload = await rpc("submit", session_id=new, call_id="stale-input", spec={
                "kind": "node", "node_type": "IndependentEcho", "inputs": {"value": {"$ref": {
                    "session": old["session_id"], "epoch": old["epoch"], "call_id": old["call_id"], "slot": 0}}}})
            assert status == 400 and payload["error"]["code"] == "Expired", payload
            assert not runtime.calls
            await rpc("close_session", session_id=new)
            report["checks"].append("handshake proposing old ID minted a different ID; old token and input locator remain invalid")

            client = await Client(url).start()
            source = client.node("IndependentEcho", value=81)
            source_ref = await source.output()
            source_id = source_ref.id
            slow = client.node("IndependentSlow", value=source_ref, delay=1.0)
            await slow._state.accepted
            deadline = time.monotonic() + 1
            state = runtime.calls[(client.session_id, slow.id)]
            while not state.inflight and time.monotonic() < deadline:
                await asyncio.sleep(.001)
            assert state.inflight
            inflight_session = client.session_id
            await client.close(wait=False)
            assert inflight_session not in runtime.session_seen and state.inflight
            with runtime.store.lock:
                assert runtime.store.entries[source_id].leases == 1
            await churn(80)
            assert inflight_session not in runtime.closed_sessions
            assert inflight_session not in runtime.session_seen
            assert state.inflight, "test did not evict diagnostics before native work ended"
            with runtime.store.lock:
                assert runtime.store.entries[source_id].leases == 1
            report["checks"].append("closed inflight session was evicted from diagnostics while its accepted input still held one lease")
            await asyncio.wait_for(runtime.queue.join(), 5)
            await asyncio.sleep(.05)
            assert not runtime.calls and not runtime.store.entries and not runtime.session_seen
            report["checks"].append("late native completion after diagnostic eviction safely reclaimed calls, output captures and input leases")
        await asyncio.sleep(.05)  # allow HTTP keepalive handlers to observe transport close
        report.update(passed=True, seconds=round(time.perf_counter() - started, 3),
                      final={"calls": len(runtime.calls), "objects": len(runtime.store.entries),
                             "active_sessions": len(runtime.session_seen), "closed_diagnostics": len(runtime.closed_sessions),
                             "request_records": len(runtime.requests), "tasks": len(asyncio.all_tasks()), "rss_mib": rss_mib()})
        args.output.write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8190)
    parser.add_argument("--output", type=Path, default=Path("tests/stress/session_lifecycle_results.json"))
    asyncio.run(main(parser.parse_args()))
