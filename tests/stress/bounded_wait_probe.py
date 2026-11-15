"""Real 140-second native call behind a 125-second upstream-read-cutoff proxy."""
import argparse
import asyncio
import gc
import json
import socket
import time
from pathlib import Path

import aiohttp
from aiohttp import web
from independent_load import Echo, Slow, rss_mib, summary

from comani import Client
from comani.daemon import NativeRuntime
from comani.daemon.http import create_app


async def await_value(value):
    return await value


async def main(args):
    for port in (args.native_port, args.proxy_port):
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)), "port occupied; no service touched"
    runtime = await NativeRuntime("/home/delva/projects/ComfyUI", cpu=True,
                                  custom_nodes=False, session_ttl=30).start()
    runtime.adapter.nodes.NODE_CLASS_MAPPINGS.update(IndependentSlow=Slow, IndependentEcho=Echo)
    native_runner = web.AppRunner(create_app(runtime))
    await native_runner.setup()
    await web.TCPSite(native_runner, "127.0.0.1", args.native_port).start()
    upstream = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=125))
    native_url = f"http://127.0.0.1:{args.native_port}"
    traces, controls = [], []
    hide_capability = False

    async def forward(request):
        start = time.perf_counter()
        body = await request.json() if request.method == "POST" else None
        row = {"method": None if body is None else body.get("method"),
               "params": None if body is None else body.get("params", {})}
        try:
            async with upstream.request(request.method, native_url + request.path, json=body) as response:
                payload = await response.json()
                if hide_capability and row["method"] == "handshake":
                    payload["result"].pop("capabilities", None)
                if row["method"] == "wait_result":
                    row["pending"] = payload.get("result", {}).get("pending") is True
                    row["terminal_ids"] = [ref["id"] for ref in payload.get("result", {}).get("outputs", [])]
                row["status"] = response.status
                return web.json_response(payload, status=response.status)
        except Exception as error:
            row["error"] = type(error).__name__ + ": " + str(error)
            raise
        finally:
            row["seconds"] = time.perf_counter() - start
            if row["method"] is not None:
                traces.append(row)

    proxy = web.Application()
    proxy.router.add_route("*", "/rpc", forward)
    proxy.router.add_get("/queue", forward)
    proxy_runner = web.AppRunner(proxy)
    await proxy_runner.setup()
    await web.TCPSite(proxy_runner, "127.0.0.1", args.proxy_port).start()
    proxy_url = f"http://127.0.0.1:{args.proxy_port}"
    client = await Client(proxy_url).start()
    stop = asyncio.Event()
    report = {"native_delay_seconds": args.delay, "proxy_read_cutoff_seconds": 125,
              "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pid": __import__("os").getpid()}
    print(json.dumps({"live": report, "native_url": native_url, "proxy_url": proxy_url}), flush=True)

    async def monitor():
        async with aiohttp.ClientSession() as http:
            while not stop.is_set():
                start = time.perf_counter()
                async with http.get(proxy_url + "/queue") as response:
                    assert response.status == 200
                    await response.json()
                controls.append(time.perf_counter() - start)
                await asyncio.sleep(.5)

    control_task = asyncio.create_task(monitor())
    try:
        assert client.handshake["capabilities"]["bounded_wait"] is True
        call = client.node("IndependentSlow", value=1234567, delay=args.delay)
        await call._state.accepted
        call_id = call.id
        session_id = client.session_id
        started = time.perf_counter()
        future = call.output()
        waiters = [asyncio.create_task(await_value(future)) for _ in range(16)]
        await asyncio.sleep(.1)
        assert not any(task.done() for task in waiters)
        for task in waiters[:4]:
            task.cancel()
        cancelled = await asyncio.gather(*waiters[:4], return_exceptions=True)
        assert all(isinstance(error, asyncio.CancelledError) for error in cancelled)
        async with aiohttp.ClientSession() as http:
            params = {"session_id": session_id, "call_id": call_id, "node_id": None, "wait_seconds": .2}
            async with http.post(proxy_url + "/rpc", json={"method": "wait_result", "params": params}) as response:
                assert (await response.json())["result"]["pending"] is True
            params["wait_seconds"] = .5
            abandoned = asyncio.create_task(http.post(proxy_url + "/rpc", json={"method": "wait_result", "params": params}))
            await asyncio.sleep(.05)
            abandoned.cancel()
            await asyncio.gather(abandoned, return_exceptions=True)
        assert not runtime.calls[(session_id, call_id)].done.cancelled()
        refs = await asyncio.wait_for(asyncio.gather(*waiters[4:]), args.delay + 30)
        elapsed = time.perf_counter() - started
        assert len({ref.id for ref in refs}) == 1
        ref_id = refs[0].id
        assert await refs[0].fetch() == 1234567
        # Repeated terminal reads return the exact same ref, never resubmit.
        terminal = [await client.transport.request("wait_result", session_id=session_id, call_id=call_id,
                                                  node_id=None, wait_seconds=20) for _ in range(3)]
        assert all(result["outputs"][0]["id"] == ref_id for result in terminal)
        sdk_queries = [row for row in traces if row["method"] == "wait_result"
                       and row["params"].get("wait_seconds") == 20]
        submissions = [row for row in traces if row["method"] == "submit" and row["params"]["call_id"] == call_id]
        assert elapsed > 125 and len(submissions) == 1
        assert sum(row["pending"] for row in sdk_queries) >= 6
        assert all(row["seconds"] < 125 for row in sdk_queries)
        assert all(row["params"]["call_id"] == call_id and row["params"].get("node_id") is None for row in sdk_queries)
        report.update(long_call_id=call_id, elapsed_seconds=round(elapsed, 3),
                      long_call_submissions=len(submissions), local_waiters=16, local_waiters_cancelled=4,
                      wait_query_count=len(sdk_queries), pending_responses=sum(row["pending"] for row in sdk_queries),
                      sdk_wait_query_times=summary([row["seconds"] for row in sdk_queries]),
                      stable_terminal_ref=ref_id, terminal_rechecks=3,
                      control_latency=summary(controls), long_task_passed=True)
        # A cancelled Task can retain the await argument through its cancellation
        # traceback. Drop the loop alias as well as the waiter container.
        del refs, future, call, waiters, terminal, cancelled, task, abandoned
        gc.collect()
        for _ in range(40):
            await asyncio.sleep(.05)
            if not runtime.calls and not runtime.store.entries:
                break
        report["after_local_gc_before_close"] = {
            "calls": len(runtime.calls), "objects": len(runtime.store.entries),
            "sdk_states": len(client._states), "sdk_watchers": len(client._watchers)}
        assert not runtime.calls and not runtime.store.entries
        await client.close()
        # Capability absent: keep one old-style read with no wait_seconds.
        hide_capability = True
        legacy = await Client(proxy_url).start()
        legacy_call = legacy.node("IndependentSlow", value=7, delay=.05)
        legacy_id = legacy_call.id
        assert await (await legacy_call.output()).fetch() == 7
        await legacy.close()
        legacy_queries = [row for row in traces if row["method"] == "wait_result"
                          and row["params"]["call_id"] == legacy_id]
        assert len(legacy_queries) == 1 and "wait_seconds" not in legacy_queries[0]["params"]
        report["capability_absent_single_legacy_query"] = True
        stop.set()
        await control_task
        await upstream.close()
        await proxy_runner.cleanup()
        await runtime.queue.join()
        await asyncio.sleep(.1)
        assert not runtime.calls and not runtime.store.entries and not runtime.session_seen
        report.update(passed=True, completed=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                      rpc_traces=traces, final={"calls": len(runtime.calls), "objects": len(runtime.store.entries),
                                             "leases": sum(entry.leases for entry in runtime.store.entries.values()),
                                             "active_sessions": len(runtime.session_seen), "sdk_watchers": len(client._watchers),
                                             "tasks": len(asyncio.all_tasks()), "rss_mib": rss_mib()})
    finally:
        stop.set()
        await asyncio.gather(control_task, return_exceptions=True)
        await client.close(wait=False)
        await upstream.close()
        await proxy_runner.cleanup()
        await native_runner.cleanup()
        args.output.write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--native-port", type=int, default=8191)
    parser.add_argument("--proxy-port", type=int, default=8192)
    parser.add_argument("--delay", type=float, default=140)
    parser.add_argument("--output", type=Path, default=Path("tests/stress/bounded_wait_results.json"))
    asyncio.run(main(parser.parse_args()))
