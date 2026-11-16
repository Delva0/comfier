"""Independent HTTP/native acceptance; no mocks or existing test imports.

Run from the repository root: .venv/bin/python -B tests/stress/independent_load.py
Dedicated loopback port must be free. CPU native executor, test-only nodes.
"""
from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path

import aiohttp
from aiohttp import web

from comani import Client, Expired, InvalidReference, PackageConflict, RemoteError, Skipped
from comani.code import prepare_code
from comani.daemon import NativeRuntime
from comani.daemon.http import create_app
from comani.transport import HTTPTransport


class Echo:
    RETURN_TYPES = ("*",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("*",)}}

    def run(self, value):
        return (value,)


class Add(Echo):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("*",), "amount": ("INT",)}}

    def run(self, value, amount):
        return (value + amount,)


class Slow(Echo):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("*",), "delay": ("FLOAT",)}}

    def run(self, value, delay):
        time.sleep(delay)
        return (value,)


class Fail(Echo):
    def run(self, value):
        raise RuntimeError(f"intentional independent failure {value}")


class Lazy(Echo):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"left": ("*", {"lazy": True}),
                             "right": ("*", {"lazy": True}), "choose_left": ("BOOLEAN",)}}

    def check_lazy_status(self, choose_left, left=None, right=None):
        name, value = ("left", left) if choose_left else ("right", right)
        return [name] if value is None else []

    def run(self, choose_left, left=None, right=None):
        return (left if choose_left else right,)


class Expand(Echo):
    def run(self, value):
        from comfy_execution.graph_utils import GraphBuilder
        graph = GraphBuilder()
        first = graph.node("IndependentAdd", return_call=True, value=value, amount=2)
        second = graph.node("IndependentAdd", return_call=True, value=first.out(0), amount=3)
        return {"result": (second.out(0),), "expand": graph.finalize()}


class Output(Echo):
    OUTPUT_NODE = True

    def run(self, value):
        return {"ui": {"text": [str(value)]}, "result": (value,)}


class Pair(Echo):
    RETURN_TYPES = ("*", "*")

    def run(self, value):
        return (value, value + 1)


def nested_sum(payload):
    return payload["left"][0] + payload["right"]["value"]


def identity(value):
    return value


def summary(values):
    values = sorted(values)
    if not values:
        return {"n": 0}
    return {"n": len(values), "p50_ms": round(statistics.median(values) * 1000, 3),
            "p95_ms": round(values[min(len(values) - 1, int(len(values) * .95))] * 1000, 3),
            "max_ms": round(max(values) * 1000, 3)}


def rss_mib():
    fields = Path("/proc/self/status").read_text().splitlines()
    return round(int(next(x.split()[1] for x in fields if x.startswith("VmRSS:"))) / 1024, 2)


class Meter:
    def __init__(self, runtime):
        self.runtime = runtime
        self.phase = "boot"
        self.rpc = defaultdict(list)
        self.control = []
        self.events = Counter()
        self.times = {}
        self.queue_wait = []
        self.execution = []
        self.samples = []
        self.waits = 0
        self.cancelled = 0
        self.original_emit = runtime._emit
        runtime._emit = self.emit

    def emit(self, kind, state=None, **data):
        now = time.perf_counter()
        self.events[(self.phase, kind)] += 1
        if state is not None:
            key = (state.session, state.call_id)
            if kind == "accepted":
                self.times[key] = [now, None]
            elif kind == "running" and key in self.times:
                self.times[key][1] = now
                self.queue_wait.append((self.phase, now - self.times[key][0]))
            elif kind in ("completed", "failed") and key in self.times:
                _, running = self.times.pop(key)
                if running is not None:
                    self.execution.append((self.phase, now - running))
        return self.original_emit(kind, state, **data)

    def snapshot(self):
        r = self.runtime
        with r.store.lock:
            entries = list(r.store.entries.values())
        return {"rss_mib": rss_mib(), "tasks": len(asyncio.all_tasks()),
                "calls": len(r.calls), "objects": len(entries),
                "leases": sum(x.leases for x in entries),
                "owned": sum(x.owner == "owned" for x in entries),
                "borrowed": sum(x.owner == "borrowed" for x in entries),
                "native_cache": len(r.adapter.executor.caches.outputs.cache),
                "sessions_seen": len(r.session_seen), "closed_sessions": len(r.closed_sessions),
                "listeners": len(r.listeners), "queue": r.queue.qsize()}

    def report(self, phase):
        return {"rpc": {method: summary(values) for (p, method), values in self.rpc.items() if p == phase},
                "control_queue": summary([v for p, v in self.control if p == phase]),
                "accepted_to_running": summary([v for p, v in self.queue_wait if p == phase]),
                "native_execution": summary([v for p, v in self.execution if p == phase]),
                "events": {kind: n for (p, kind), n in self.events.items() if p == phase},
                "peak_rss_mib": max((s["rss_mib"] for p, s in self.samples if p == phase), default=rss_mib()),
                "peak_tasks": max((s["tasks"] for p, s in self.samples if p == phase), default=0)}


class TimedTransport(HTTPTransport):
    def __init__(self, url, meter):
        super().__init__(url, timeout=180)
        self.meter = meter

    async def request(self, method, **params):
        start = time.perf_counter()
        phase = self.meter.phase
        try:
            return await super().request(method, **params)
        finally:
            self.meter.rpc[(phase, method)].append(time.perf_counter() - start)


async def waiter(value, meter):
    meter.waits += 1
    return await value


async def assert_output(call, expected, meter, *, fetch=True):
    future = call.output()
    tasks = [asyncio.create_task(waiter(future, meter)) for _ in range(4)]
    await asyncio.sleep(0)  # enter __await__ before cancellation
    did_cancel = tasks[0].cancel()
    meter.cancelled += int(did_cancel)
    cancelled = await asyncio.gather(tasks[0], return_exceptions=True)
    if did_cancel:
        assert isinstance(cancelled[0], asyncio.CancelledError)
    refs = await asyncio.gather(*tasks[1:])
    assert refs[0].id == refs[1].id == refs[2].id
    if fetch:
        assert await refs[0].fetch() == expected
    if int(call.id[-1], 16) % 4 == 0:
        aliases = await asyncio.gather(waiter(call, meter), waiter(call, meter))
        assert aliases[0][0].id == refs[0].id == aliases[1][0].id


async def make_graph(client):
    with client.graph() as sub:
        value = sub.input("value", type="*")
        early, = await client.node("IndependentAdd", return_call=True, value=value, amount=7)
        expanded = client.node("IndependentExpand", return_call=True, value=early)
        bad = client.node("IndependentFail", return_call=True, value=-999)
        selected = client.node("IndependentLazy", return_call=True, left=expanded.output(), right=bad.output(), choose_left=True)
        computed = client.python(nested_sum, inputs={"payload": {"left": [selected.output()], "right": {"value": early}}})
        sub.output("value", computed.output())
    with client.graph() as graph:
        value = graph.input("value", type="*")
        combined = client.submit(sub, inputs={"value": value})
        final = client.node("IndependentAdd", return_call=True, value=combined.output("value"), amount=1)
        graph.output("value", final.output())
        graph.output("alias", final.output())
    return graph


async def load_session(index, rounds, url, meter):
    client = await Client(transport=TimedTransport(url, meter)).start()
    try:
        graph = await make_graph(client)
        calls, checks = [], []
        for i in range(rounds):
            n = index * 100000 + i
            root = client.node("IndependentEcho", return_call=True, value=n)
            py = client.python(nested_sum, inputs={"payload": {"left": [root.output()], "right": {"value": root.output()}}})
            final = client.node("IndependentAdd", return_call=True, value=py.output(), amount=1)
            calls.extend((root, py, final))
            checks.extend((assert_output(root, n, meter), assert_output(py, n * 2, meter), assert_output(final, n * 2 + 1, meter)))
            if i % 4 == 0:
                run = client.submit(graph, inputs={"value": root.output()})
                owned = client.python(identity, inputs={"value": run.output("value")})
                calls.extend((run, owned))
                checks.extend((assert_output(run, None, meter, fetch=False), assert_output(owned, n * 2 + 20, meter)))
                raw = {"a": {"class_type": "IndependentEcho", "inputs": {"value": n}},
                       "b": {"class_type": "IndependentExpand", "inputs": {"value": ["a", 0]}},
                       "c": {"class_type": "IndependentAdd", "inputs": {"value": ["b", 0], "amount": 9}}}
                rawrun = client.submit(raw, outputs={"answer": ["c", 0]}, targets=["c"])
                rawowned = client.python(identity, inputs={"value": rawrun.output("answer")})
                calls.extend((rawrun, rawowned))
                checks.extend((assert_output(rawrun, None, meter, fetch=False), assert_output(rawowned, n + 14, meter)))
        results = await asyncio.gather(*checks, return_exceptions=True)
        failures = [f"{type(e).__name__}: {e}" for e in results if isinstance(e, BaseException)]
        count = len(calls)
        calls.clear()
        checks.clear()
        results.clear()
        del root, py, final, run, owned, rawrun, rawowned
        gc.collect()
        for _ in range(40):
            await asyncio.sleep(.05)
            remaining_calls = sum(s == client.session_id for s, _ in meter.runtime.calls)
            with meter.runtime.store.lock:
                remaining_objects = sum(e.session == client.session_id for e in meter.runtime.store.entries.values())
            if remaining_calls == 0 and remaining_objects == 0:
                break
        return {"session": index, "calls": count, "checks": count, "unexpected": failures,
                "watchers_after": len(client._watchers), "local_states_after": len(client._states),
                "remote_calls_after_gc_before_close": remaining_calls,
                "remote_objects_after_gc_before_close": remaining_objects}
    finally:
        await client.close(wait=False)


async def control_loop(url, meter, stop):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        while not stop.is_set():
            phase = meter.phase
            start = time.perf_counter()
            async with session.get(url + "/queue") as response:
                assert response.status == 200
                await response.json()
            meter.control.append((phase, time.perf_counter() - start))
            meter.samples.append((phase, meter.snapshot()))
            await asyncio.sleep(.02)


async def settle(runtime, timeout=10):
    await asyncio.wait_for(runtime.queue.join(), timeout)
    for _ in range(20):
        gc.collect()
        await asyncio.sleep(.05)
        if not runtime.calls and not runtime.store.entries:
            return


async def semantic_phase(url, runtime, meter):
    notes = []
    async with Client(transport=TimedTransport(url, meter)) as client:
        barrier = client.node("IndependentSlow", return_call=True, value=321, delay=.2)
        local_await = asyncio.create_task(waiter(barrier.output(), meter))
        dependent = client.node("IndependentEcho", return_call=True, value=barrier.output())
        unrelated = client.node("IndependentEcho", return_call=True, value=654)
        await unrelated._state.accepted
        await asyncio.sleep(.01)
        assert not local_await.done()
        assert runtime.calls[(client.session_id, dependent.id)].status == "accepted"
        assert runtime.calls[(client.session_id, unrelated.id)].status == "queued"
        assert await (await unrelated.output()).fetch() == 654
        assert await (await local_await).fetch() == 321
        assert await (await dependent.output()).fetch() == 321
        notes.append("local await leaves independent call queued while its sibling waits on inputs")
        before = len(runtime.calls)
        with client.graph() as graph:
            early, = await client.node("IndependentEcho", return_call=True, value=47)
            slow = client.node("IndependentSlow", return_call=True, value=early, delay=.2)
            graph.output("value", slow.output())
        assert len(runtime.calls) == before, "capture unexpectedly executed"
        run = client.submit(graph)
        early_ref = await run[early]
        assert await early_ref.fetch() == 47
        assert not run._state.done.done(), "middle ref failed to arrive before whole invocation"
        waiters = [asyncio.create_task(waiter(run.output(), meter)) for _ in range(6)]
        await asyncio.sleep(0)
        assert not any(task.done() for task in waiters)
        for task in waiters[:2]:
            task.cancel()
            meter.cancelled += 1
        cancelled = await asyncio.gather(*waiters[:2], return_exceptions=True)
        assert all(isinstance(error, asyncio.CancelledError) for error in cancelled)
        owned = client.python(identity, inputs={"value": run.output("value")})
        ownedref = await owned.output()
        assert ownedref.ownership == "owned"
        assert await ownedref.fetch() == 47
        borrowed = await run.output("value")
        siblings = await asyncio.gather(*waiters[2:])
        assert all(ref.id == borrowed.id for ref in siblings)
        notes.append("in-progress cancellation of 2/6 same-future waiters preserved siblings")
        assert borrowed.ownership == "borrowed"
        runtime.adapter.executor.caches.outputs.cache.clear()
        try:
            await borrowed.fetch()
        except Expired:
            notes.append("borrowed cache eviction -> Expired")
        else:
            raise AssertionError("borrowed output remained alive after cache eviction")
        assert await ownedref.fetch() == 47
        try:
            await ownedref
        except TypeError:
            notes.append("ready reference is not awaitable")
        else:
            raise AssertionError("ready reference was awaitable")
        pair = client.node("IndependentPair", return_call=True, value=12)
        both = await asyncio.gather(*(waiter(pair.output(slot), meter) for slot in (0, 1) for _ in range(4)))
        assert len({ref.id for ref in both[:4]}) == 1
        assert len({ref.id for ref in both[4:]}) == 1
        assert both[0].id != both[4].id
        first, second = await pair
        consumer = client.node("IndependentSlow", return_call=True, value=first, delay=.03)
        await consumer._state.accepted
        first_id = first.id
        first.release()
        assert await (await consumer.output()).fetch() == 12
        assert await second.fetch() == 13
        await asyncio.sleep(.05)
        assert first_id not in runtime.store.entries
        bad = client.node("IndependentFail", return_call=True, value=8)
        downstream = client.node("IndependentEcho", return_call=True, value=bad.output())
        independent = client.node("IndependentEcho", return_call=True, value=99)
        failures = await asyncio.gather(waiter(bad, meter), waiter(downstream, meter), return_exceptions=True)
        assert all(isinstance(e, RemoteError) for e in failures)
        assert await (await independent.output()).fetch() == 99
        with client.graph() as lazygraph:
            selected = client.node("IndependentExpand", return_call=True, value=11)
            unselected = client.node("IndependentFail", return_call=True, value=99)
            choice = client.node("IndependentLazy", return_call=True, left=selected.output(), right=unselected.output(), choose_left=True)
            lazygraph.output("chosen", choice.output())
        lazyrun = client.submit(lazygraph)
        assert await (await lazyrun.output()).fetch() == 16
        try:
            await lazyrun[unselected.output()]
        except Skipped:
            notes.append("native lazy/expansion -> 16; unselected -> Skipped")
        else:
            raise AssertionError("unselected branch did not raise Skipped")
        async with Client(transport=TimedTransport(url, meter)) as other:
            try:
                other.node("IndependentEcho", return_call=True, value=second)
            except InvalidReference:
                notes.append("SDK cross-session ref rejected")
            else:
                raise AssertionError("cross-session reference accepted")
            try:
                await other.transport.request("fetch", session_id=other.session_id, ref_id=second.id, epoch=client.epoch)
            except Expired:
                notes.append("wire cross-session token rejected")
            else:
                raise AssertionError("wire cross-session token fetched")
        image = client.node("EmptyImage", return_call=True, width=16, height=16, batch_size=1, color=0)
        invert = client.node("ImageInvert", return_call=True, image=image.output())
        tensor_summary = client.python(lambda x: (tuple(x.shape), float(x.mean())), inputs={"x": invert.output()})
        assert await (await tensor_summary.output()).fetch() == ((1, 16, 16, 3), 1.0)
        notes.append("built-in native CPU EmptyImage/ImageInvert plus Python verified")
    await settle(runtime)
    return notes


async def raw_ws_ttl_phase(url, runtime, meter):
    notes = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        ws = await session.ws_connect(url + "/ws")
        event_task = asyncio.create_task(ws.receive_json())
        raw = {"source": {"class_type": "IndependentEcho", "inputs": {"value": 123}},
               "sink": {"class_type": "IndependentOutput", "inputs": {"value": ["source", 0]}}}
        async with session.post(url + "/prompt", json={"prompt": raw}) as response:
            assert response.status == 200
            accepted = await response.json()
        prompt_id = accepted["prompt_id"]
        event = await asyncio.wait_for(event_task, 5)
        assert event["call_id"] == prompt_id
        for _ in range(100):
            async with session.get(url + "/history/" + prompt_id) as response:
                history = await response.json()
            if history:
                break
            await asyncio.sleep(.01)
        assert history[prompt_id]["status"]["completed"]
        assert history[prompt_id]["outputs"]["sink"]["text"] == ["123"]
        await ws.close()
        await asyncio.sleep(.1)
        assert not runtime.listeners
        notes.append("real /prompt,/history,/queue,/ws completed; WS listener reclaimed")
        # Session identifiers are now minted by handshake rather than adopted
        # from an arbitrary request. This client intentionally sends no heartbeat.
        async with session.post(url + "/rpc", json={"method": "handshake", "params": {}}) as response:
            assert response.status == 200
            transient = (await response.json())["result"]["session_id"]
        async with session.post(url + "/rpc", json={"method": "submit", "params": {
                "session_id": transient, "call_id": "slow", "spec": {"kind": "node", "node_type": "IndependentSlow", "inputs": {"value": 1, "delay": .05}}}}) as response:
            assert response.status == 200
        abandoned = aiohttp.ClientSession()
        request = asyncio.create_task(abandoned.post(url + "/rpc", json={"method": "wait_result", "params": {"session_id": transient, "call_id": "slow"}}))
        await asyncio.sleep(.005)
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await abandoned.close()
        deadline = time.monotonic() + runtime.session_ttl + min(10, max(.02, runtime.session_ttl / 3)) + 3
        while transient not in runtime.closed_sessions and time.monotonic() < deadline:
            await asyncio.sleep(.05)
        assert transient in runtime.closed_sessions
        assert not any(e.session == transient for e in runtime.store.entries.values())
        assert not any(s == transient for s, _ in runtime.calls)
        notes.append("disconnected long-poll session TTL reclaimed calls and objects")
    await runtime.close_session("raw")
    await settle(runtime)
    return notes


async def package_phase(url, runtime, meter):
    with tempfile.TemporaryDirectory(prefix="comani-independent-load-") as scratch:
        root = Path(scratch)
        package = root / "a" / "independent_probe_package"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("from .operations import tick\n")
        (package / "state.py").write_text("count = 0\n")
        (package / "helper.py").write_text("def plus(value):\n    return value + 5\n")
        (package / "operations.py").write_text(
            "from independent_probe_package import state\nfrom .helper import plus\n"
            "def tick(value):\n    state.count += 1\n    return state.count, plus(value)\n")
        clone = root / "b" / package.name
        shutil.copytree(package, clone)
        before = prepare_code(source=package, entry="operations:tick").artifact_id
        for file in clone.rglob("*.py"):
            os.utime(file, (1234567890, 1234567890))
            file.chmod(0o755)
        after = prepare_code(source=clone, entry="operations:tick").artifact_id
        assert before == after, "package hash changed with mtime/mode only"
        source_file = root / "source_function.py"
        source_file.write_text("def apply(value):\n    return value * 3 + 1\n")

        async def package_session(index):
            async with Client(transport=TimedTransport(url, meter)) as client:
                selected = package if index % 2 else clone
                calls = [client.python(source=selected, entry="operations:tick", inputs={"value": index}) for _ in range(32)]
                with client.graph() as graph:
                    seed = graph.input("seed", type="*")
                    file_call = client.python(source=source_file, entry="apply", inputs={"value": seed})
                    graph.output("value", file_call.output())
                graph_run = client.submit(graph, inputs={"seed": index})
                owned = client.python(identity, inputs={"value": graph_run.output()})
                refs = await asyncio.gather(*(waiter(call.output(), meter) for call in calls))
                values = await asyncio.gather(*(ref.fetch() for ref in refs))
                assert all(isinstance(value, tuple) and value[1] == index + 5 for value in values)
                assert await (await owned.output()).fetch() == index * 3 + 1
                return [value[0] for value in values]

        values = await asyncio.gather(*(package_session(i) for i in range(8)))
        assert sorted(value for group in values for value in group) == list(range(1, 257))
        async with Client(transport=TimedTransport(url, meter)) as client:
            reused = client.python(source=package, entry="operations:tick", inputs={"value": 0})
            assert await (await reused.output()).fetch() == (257, 5)
            (clone / "helper.py").write_text("def plus(value):\n    return value + 999\n")
            conflict = client.python(source=clone, entry="operations:tick", inputs={"value": 0})
            try:
                await conflict
            except PackageConflict:
                pass
            else:
                raise AssertionError("different content reused an already-loaded package name")
            installed = root / "aiohttp"
            installed.mkdir()
            (installed / "__init__.py").write_text("def apply():\n    return 999\n")
            collision = client.python(source=installed, entry="aiohttp:apply")
            try:
                await collision
            except PackageConflict:
                pass
            else:
                raise AssertionError("uploaded package replaced installed aiohttp")
    await settle(runtime)
    return ["8 HTTP sessions x 32 package calls: absolute+relative imports work",
            "256 unique shared process-global increments, despite closing client sessions",
            "mtime/mode-only changes preserve content hash and globals",
            "source file inside native captured graph returns correct value",
            "same-name different content and installed aiohttp conflicts -> PackageConflict"]


async def large_graph_phase(url, runtime, meter):
    async def run_session(index):
        async with Client(transport=TimedTransport(url, meter)) as client:
            with client.graph() as inner:
                value = inner.input("value", type="*")
                current = value
                for step in range(32):
                    first = client.node("IndependentAdd", return_call=True, value=current, amount=1)
                    py = client.python(nested_sum, inputs={"payload": {"left": [first.output()], "right": {"value": 1}}})
                    expanded = client.node("IndependentExpand", return_call=True, value=py.output())
                    current = expanded.output()
                    if step % 4 == 0:
                        never = client.node("IndependentFail", return_call=True, value=step)
                        current = client.node("IndependentLazy", return_call=True, left=current, right=never.output(), choose_left=True).output()
                inner.output("value", current)
            with client.graph() as outer:
                value = outer.input("value", type="*")
                first = client.submit(inner, inputs={"value": value})
                second = client.submit(inner, inputs={"value": first.output()})
                outer.output("value", second.output())
                outer.output("alias", second.output())
            assert len(outer.nodes) == 224
            calls, checks = [], []
            for repeat in range(4):
                n = index * 10000 + repeat
                run = client.submit(outer, inputs={"value": n})
                owned = client.python(identity, inputs={"value": run.output("alias")})
                calls.extend((run, owned))
                checks.extend((assert_output(run, None, meter, fetch=False), assert_output(owned, n + 448, meter)))
            results = await asyncio.gather(*checks, return_exceptions=True)
            errors = [str(error) for error in results if isinstance(error, BaseException)]
            assert not errors, errors
            return len(calls)

    rows = await asyncio.gather(*(run_session(index) for index in range(8)))
    await settle(runtime)
    return [f"8 sessions x 4 invocations x 224 declared nodes: 7168 declared nodes in 32 large composed graphs; {sum(rows)} HTTP invocations",
            "each graph: two composed 32-step native/Python/expansion chains, 16 unselected failing branches, two aliased outputs",
            "all 32 owned outputs equal input+448; in-progress waiter cancellation preserved all siblings"]


async def main(args):
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", args.port)), "port already occupied; no service touched"
    url = f"http://127.0.0.1:{args.port}"
    runtime = NativeRuntime(args.comfy_root, cpu=True, custom_nodes=False, session_ttl=args.session_ttl)
    await runtime.start()
    runtime.adapter.nodes.NODE_CLASS_MAPPINGS.update({"Independent" + cls.__name__: cls for cls in (Echo, Add, Slow, Fail, Lazy, Expand, Output, Pair)})
    meter = Meter(runtime)
    runner = web.AppRunner(create_app(runtime))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", args.port).start()
    stop = asyncio.Event()
    monitor = asyncio.create_task(control_loop(url, meter, stop))
    report = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pid": os.getpid(),
              "url": url, "python": sys.version, "comfy_commit": subprocess.check_output(["git", "-C", args.comfy_root, "rev-parse", "HEAD"], text=True).strip(),
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "mode": "CPU; real native ComfyUI single worker; SDK requests over loopback HTTP",
              "session_ttl": runtime.session_ttl,
              "phases": [], "initial": meter.snapshot()}
    print(json.dumps({"live": {"pid": os.getpid(), "url": url, "report": str(args.output)}}), flush=True)
    try:
        for name, operation in [("semantics", lambda: semantic_phase(url, runtime, meter)),
                                ("raw_ws_disconnect_ttl", lambda: raw_ws_ttl_phase(url, runtime, meter)),
                                ("python_packages_8_sessions", lambda: package_phase(url, runtime, meter)),
                                ("large_graphs_8_sessions", lambda: large_graph_phase(url, runtime, meter))]:
            meter.phase = name
            start = time.perf_counter()
            try:
                details = await asyncio.wait_for(operation(), 180)
                entry = {"name": name, "passed": True, "details": details}
            except Exception:
                entry = {"name": name, "passed": False, "error": traceback.format_exc()}
            entry.update(seconds=round(time.perf_counter() - start, 3), after=meter.snapshot(), metrics=meter.report(name))
            report["phases"].append(entry)
            args.output.write_text(json.dumps(report, indent=2))
            print(json.dumps(entry), flush=True)
        for sessions in args.sessions:
            name = f"load_{sessions}_sessions"
            meter.phase = name
            start = time.perf_counter()
            before_waits, before_cancel = meter.waits, meter.cancelled
            rows = await asyncio.wait_for(asyncio.gather(*(load_session(i, args.rounds, url, meter) for i in range(sessions)), return_exceptions=True), 300)
            await settle(runtime)
            failures = [traceback.format_exception_only(type(r), r)[-1].strip() for r in rows if isinstance(r, BaseException)]
            valid = [r for r in rows if isinstance(r, dict)]
            failures.extend(e for row in valid for e in row["unexpected"])
            seconds = time.perf_counter() - start
            entry = {"name": name, "passed": not failures, "sessions": sessions,
                     "rounds_per_session": args.rounds, "calls": sum(r["calls"] for r in valid),
                     "waits_started": meter.waits - before_waits, "waiters_cancelled": meter.cancelled - before_cancel,
                     "unexpected_count": len(failures), "unexpected_samples": failures[:20],
                     "seconds": round(seconds, 3), "calls_per_second": round(sum(r["calls"] for r in valid) / seconds, 2),
                     "sessions_with_local_states_after": sum(r["local_states_after"] > 0 for r in valid),
                     "sessions_with_remote_calls_after_gc_before_close": sum(r["remote_calls_after_gc_before_close"] > 0 for r in valid),
                     "sessions_with_remote_objects_after_gc_before_close": sum(r["remote_objects_after_gc_before_close"] > 0 for r in valid),
                     "after": meter.snapshot(), "metrics": meter.report(name)}
            if entry["sessions_with_remote_calls_after_gc_before_close"] or entry["sessions_with_remote_objects_after_gc_before_close"]:
                entry["passed"] = False
                entry["gc_failure"] = "SDK session still open: call/object registry did not clear within two seconds after local GC"
            if entry["after"]["calls"] or entry["after"]["objects"]:
                entry["passed"] = False
                entry["cleanup_failure"] = "call/object registry not empty after closing every SDK session"
            report["phases"].append(entry)
            args.output.write_text(json.dumps(report, indent=2))
            print(json.dumps(entry), flush=True)
    finally:
        stop.set()
        await monitor
        report["final_before_shutdown"] = meter.snapshot()
        report["all_passed"] = all(p["passed"] for p in report["phases"])
        report["completed"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        args.output.write_text(json.dumps(report, indent=2))
        await runner.cleanup()
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comfy-root", default="/home/delva/projects/ComfyUI")
    parser.add_argument("--port", type=int, default=8190)
    parser.add_argument("--sessions", type=int, nargs="+", default=[1, 8, 32])
    parser.add_argument("--session-ttl", type=float, default=1.5)
    parser.add_argument("--rounds", type=int, default=32)
    parser.add_argument("--output", type=Path, default=Path("tests/stress/independent_load_results.json"))
    sys.exit(asyncio.run(main(parser.parse_args())))
