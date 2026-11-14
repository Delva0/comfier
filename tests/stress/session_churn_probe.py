"""Low-resource real HTTP/native session churn and post-TTL metadata observation."""
import argparse
import asyncio
import json
import socket
import time
from pathlib import Path

from aiohttp import web
from independent_load import Echo, rss_mib

from comani import Client
from comani._runtime import NativeRuntime
from comani._runtime.http import create_app


async def main(args):
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", args.port))
    runtime = await NativeRuntime("/home/delva/projects/ComfyUI", cpu=True,
                                  custom_nodes=False, session_ttl=.3).start()
    runtime.adapter.nodes.NODE_CLASS_MAPPINGS.update(IndependentEcho=Echo)
    runner = web.AppRunner(create_app(runtime))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", args.port).start()
    url = f"http://127.0.0.1:{args.port}"
    samples = []

    def snapshot(completed, label):
        with runtime.store.lock:
            entries = list(runtime.store.entries.values())
        active_states = [state for state in runtime.calls.values() if state.inflight]
        return {"label": label, "completed_sessions": completed,
                "closed_sessions": len(runtime.closed_sessions), "seen_sessions": len(runtime.session_seen),
                "calls": len(runtime.calls), "objects": len(entries), "leases": sum(e.leases for e in entries),
                "inflight_calls": len(active_states), "requests_records": len(runtime.requests),
                "tasks": len(asyncio.all_tasks()), "rss_mib": rss_mib()}

    async def one_session(index):
        async with Client(url) as client:
            call = client.node("IndependentEcho", value=index)
            ref = await call.output()
            assert await ref.fetch() == index

    report = {"ttl_seconds": runtime.session_ttl, "snapshots": samples}
    started = time.perf_counter()
    try:
        samples.append(snapshot(0, "baseline"))
        completed = 0
        for milestone in (100, 300, 1000):
            while completed < milestone:
                batch = min(10, milestone - completed)
                await asyncio.gather(*(one_session(i) for i in range(completed, completed + batch)))
                completed += batch
            await runtime.queue.join()
            await asyncio.sleep(.05)
            samples.append(snapshot(completed, "closed_batch"))
        for periods in (1, 3, 10):
            await asyncio.sleep(runtime.session_ttl * periods)
            samples.append(snapshot(completed, f"after_additional_{periods}_ttl"))
        report.update(seconds=round(time.perf_counter() - started, 3),
                      no_calls_or_objects_retained=not runtime.calls and not runtime.store.entries,
                      closed_metadata_after_14_ttl=len(runtime.closed_sessions),
                      seen_metadata_after_14_ttl=len(runtime.session_seen))
        args.output.write_text(json.dumps(report, indent=2))
        print(json.dumps(report), flush=True)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8190)
    parser.add_argument("--output", type=Path, default=Path("tests/stress/session_churn_results.json"))
    asyncio.run(main(parser.parse_args()))
