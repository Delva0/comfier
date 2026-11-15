"""Real HTTP transport against the native runtime, beyond SDK unit mocks."""

import asyncio
import contextlib
import gc
import time
import weakref

import pytest
from aiohttp import ClientSession

from comani import Client, Expired, PackageConflict, RemoteError, RemoteRef
from comani.code import prepare_code

pytestmark = pytest.mark.native


async def eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.01)


def owned_count(runtime, session):
    with runtime.store.lock:
        return sum(e.owner == "owned" and e.session == session for e in runtime.store.entries.values())


async def delayed(value, delay=0.2):
    await asyncio.sleep(delay)
    return value


def blocked(value, delay=0.5):
    import time

    time.sleep(delay)
    return value


async def test_http_call_runs_without_await_and_keeps_one_object_slot(native_http):
    runtime, url = native_http
    async with Client(url) as remote:
        call = remote.python(lambda: {"items": [1, 2], "pair": (3, 4)})
        await eventually(lambda: call.status in {"completed", "failed"})
        value, = await call
        assert isinstance(value, RemoteRef)
        assert await value.fetch() == {"items": [1, 2], "pair": (3, 4)}
        assert runtime.calls[(remote.session_id, call.id)].status == "succeeded"


async def test_http_submission_remains_responsive_with_pending_dependency(native_http):
    runtime, url = native_http
    async with Client(url) as remote:
        first = remote.python(delayed, args=(7, 0.4))
        dependent = remote.python(lambda value: value + 1, inputs={"value": first.output()})
        await eventually(lambda: (remote.session_id, dependent.id) in runtime.calls)
        assert not runtime.calls[(remote.session_id, first.id)].done.done()
        assert await (await dependent.output()).fetch() == 8


async def test_sync_native_work_does_not_block_submit_or_heartbeat(native_http):
    runtime, url = native_http
    async with Client(url) as remote:
        first = remote.python(blocked, args=(4, 0.6))
        await eventually(lambda: runtime.calls.get((remote.session_id, first.id)) is not None
                         and runtime.calls[(remote.session_id, first.id)].status == "running")
        second = remote.python(lambda: 9)
        await asyncio.wait_for(
            remote.transport.request("heartbeat", session_id=remote.session_id), timeout=0.3,
        )
        await eventually(lambda: (remote.session_id, second.id) in runtime.calls, timeout=0.3)
        assert not runtime.calls[(remote.session_id, first.id)].done.done()
        assert await (await second.output()).fetch() == 9


@pytest.mark.parametrize("native_http", [{"session_ttl": 0.15}], indirect=True)
async def test_active_http_wait_renews_session_then_disconnect_expires(native_http):
    runtime, url = native_http
    remote = await Client(url).start()
    try:
        # Isolate the active HTTP request guard from the SDK heartbeat.
        await remote._lease.close()
        call = remote.python(blocked, args=(31, 0.5))
        ref = await call.output()
        assert await ref.fetch() == 31
        session = remote.session_id
        assert session not in runtime.closed_sessions
        await remote.transport.close()  # abrupt disconnect, no close_session RPC
        await eventually(lambda: session in runtime.closed_sessions, timeout=2)
        assert not any(key[0] == session for key in runtime.calls)
        assert not any(entry.session == session for entry in runtime.store.entries.values())
    finally:
        with contextlib.suppress(Exception):
            await remote.close(wait=False)


async def test_workflow_intermediate_ready_before_whole_workflow(native_http):
    runtime, url = native_http
    async with Client(url) as remote:
        with remote.graph() as graph:
            intermediate, = await remote.python(lambda: 17)
            slow = remote.python(delayed, args=(intermediate, 0.5))
            graph.output("final", slow.output())
        run = remote.submit(graph)
        value = await asyncio.wait_for(run[intermediate], timeout=10)
        assert value.ownership == "borrowed"
        assert not runtime.calls[(remote.session_id, run.id)].done.done()
        assert await value.fetch() == 17
        assert await (await run.output("final")).fetch() == 17


async def test_gc_does_not_release_queued_consumer_input(native_http):
    runtime, url = native_http
    async with Client(url) as remote:
        producer = remote.python(lambda: {"items": [2, 3]})
        value = producer.output()
        consumer = remote.python(delayed, args=([value, value], 0.2))
        consumer_id = consumer.id
        await eventually(lambda: (remote.session_id, consumer_id) in runtime.calls)
        del producer, value
        gc.collect()
        result = await consumer.output()
        assert await result.fetch() == [{"items": [2, 3]}, {"items": [2, 3]}]
        del consumer, result
        gc.collect()
        await eventually(lambda: owned_count(runtime, remote.session_id) == 0)


async def test_completed_call_records_and_objects_are_collected(native_http):
    runtime, url = native_http
    async with Client(url) as remote:
        for index in range(20):
            call = remote.python(lambda value: {"value": value}, inputs={"value": index})
            ref = await call.output()
            assert await ref.fetch() == {"value": index}
            del call, ref
        gc.collect()
        await eventually(lambda: not any(key[0] == remote.session_id for key in runtime.calls))
        await eventually(lambda: not any(e.session == remote.session_id
                                       for e in runtime.store.entries.values()))


async def test_workflow_borrowed_value_expires_after_native_cache_clear(native_http):
    runtime, url = native_http
    async def returned_borrow():
        remote = await Client(url).start()
        with remote.graph() as graph:
            value, = await remote.python(lambda: [5, 6])
            graph.output("value", value)
        run = remote.submit(graph)
        ref = await run.output("value")
        return ref, weakref.ref(remote), remote._lease, remote.session_id

    ref, client_ref, lease, session = await returned_borrow()
    try:
        gc.collect()
        assert client_ref() is not None and ref.ownership == "borrowed"
        assert await ref.fetch() == [5, 6]
        runtime.adapter.executor.caches.outputs.cache.clear()
        with pytest.raises(Expired):
            await ref.fetch()
        assert client_ref() is not None
        ref.release()
        gc.collect()
        await eventually(lambda: client_ref() is None and lease.done.done()
                         and session not in runtime.session_seen)
    finally:
        remaining = client_ref()
        if remaining is not None:
            await remaining.close(wait=False)


async def test_workflow_reuses_eager_object_and_native_prompt(native_http):
    _, url = native_http
    async with Client(url) as remote:
        eager = remote.python(lambda: 11)
        with remote.graph() as graph:
            value = graph.input("value")
            changed, = await remote.python(lambda value: value * 2, inputs={"value": value})
            graph.output("changed", changed)
        run = remote.submit(graph, inputs={"value": eager.output()})
        assert await (await run.output("changed")).fetch() == 22
        raw = {
            "1": {"class_type": "EmptyLatentImage", "inputs": {
                "width": 64, "height": 64, "batch_size": 1,
            }},
        }
        raw_run = remote.submit(raw, outputs={"latent": ["1", 0]}, targets=["1"])
        latent = await (await raw_run.output("latent")).fetch()
        assert tuple(latent["samples"].shape) == (1, 4, 8, 8)


async def test_python_file_and_package_artifacts(native_http, tmp_path):
    _, url = native_http
    source = tmp_path / "transform.py"
    source.write_text("def apply(value):\n    return {'value': value + 2}\n")
    package = tmp_path / "transform_package"
    package.mkdir()
    (package / "__init__.py").write_text("from .operation import apply\n")
    (package / "operation.py").write_text("def apply(value):\n    return (value, [value + 3])\n")
    async with Client(url) as remote:
        file_call = remote.python(source=source, entry="apply", inputs={"value": 5})
        assert await (await file_call.output()).fetch() == {"value": 7}
        package_call = remote.python(source=package, entry="transform_package:apply", inputs={"value": 5})
        assert await (await package_call.output()).fetch() == (5, [8])


async def test_composed_graph_runs_file_and_package_python(native_http, tmp_path):
    _, url = native_http
    source = tmp_path / "add.py"
    source.write_text("def apply(value):\n    return value + 2\n")
    package = tmp_path / "triple"
    package.mkdir()
    (package / "__init__.py").write_text("def apply(value):\n    return value * 3\n")
    async with Client(url) as remote:
        with remote.graph() as inner:
            value = inner.input("value")
            changed, = await remote.python(source=source, entry="apply", inputs={"value": value})
            inner.output("changed", changed)
        with remote.graph() as outer:
            value = outer.input("value")
            first = remote.submit(inner, inputs={"value": value})
            second = remote.submit(inner, inputs={"value": first.output("changed")})
            final = remote.python(source=package, entry="triple:apply",
                                  inputs={"value": second.output("changed")})
            outer.output("final", final.output())
        run = remote.submit(outer, inputs={"value": 3})
        assert await (await run.output("final")).fetch() == 21


async def test_package_normal_imports_globals_and_conflicts(native_http, tmp_path):
    import os
    import shutil
    import stat

    _, url = native_http
    package = tmp_path / "comani_package_a_fixture"
    package.mkdir()
    (package / "__init__.py").write_text("from .ops import apply\n")
    (package / "state.py").write_text("count = 0\n")
    (package / "helpers.py").write_text("OFFSET = 2\n")
    (package / "ops.py").write_text(
        "import asyncio\n"
        "from comani_package_a_fixture import state\n"  # normal absolute import
        "from .helpers import OFFSET\n"              # normal relative import
        "async def apply(value):\n"
        "    state.count += 1\n"
        "    await asyncio.sleep(0)\n"
        "    return {'value': value + OFFSET, 'count': state.count}\n"
    )
    original_hash = prepare_code(source=package, entry="ops:apply").artifact_id
    async with Client(url) as remote:
        first = remote.python(source=package, entry="ops:apply", inputs={"value": 3})
        assert await (await first.output()).fetch() == {"value": 5, "count": 1}
        for path in package.glob("*.py"):
            os.utime(path, (1_700_000_000, 1_700_000_000))
            path.chmod(path.stat().st_mode ^ stat.S_IXUSR)
        assert prepare_code(source=package, entry="ops:apply").artifact_id == original_hash
        second = remote.python(source=package, entry="comani_package_a_fixture.ops:apply",
                               inputs={"value": 4})
        assert await (await second.output()).fetch() == {"value": 6, "count": 2}
    async with Client(url) as remote:
        third = remote.python(source=package, entry="ops:apply", inputs={"value": 5})
        assert await (await third.output()).fetch() == {"value": 7, "count": 3}
        other = tmp_path / "different" / package.name
        shutil.copytree(package, other)
        (other / "helpers.py").write_text("OFFSET = 999\n")
        conflict = remote.python(source=other, entry="ops:apply", inputs={"value": 0})
        with pytest.raises(PackageConflict, match="(?i)conflict|already|different"):
            await conflict
        intact = remote.python(source=package, entry="ops:apply", inputs={"value": 6})
        assert await (await intact.output()).fetch() == {"value": 8, "count": 4}
        installed = tmp_path / "cloudpickle"
        installed.mkdir()
        (installed / "__init__.py").write_text("def apply():\n    return 'not the installed package'\n")
        collision = remote.python(source=installed, entry="cloudpickle:apply")
        with pytest.raises(PackageConflict, match="(?i)conflict|installed|already"):
            await collision
        version = remote.python(lambda: __import__("cloudpickle").__version__)
        assert await (await version.output()).fetch() == "3.1.2"


async def test_native_prompt_history_ui_events_and_hidden_prompt(native_http, tmp_path):
    import json

    import folder_paths
    from PIL import Image

    _, url = native_http
    previous_output = folder_paths.get_output_directory()
    folder_paths.set_output_directory(str(tmp_path))
    prompt = {
        "image": {"class_type": "EmptyImage", "inputs": {
            "width": 64, "height": 64, "batch_size": 1, "color": 0x123456,
        }},
        "save": {"class_type": "SaveImage", "inputs": {
            "images": ["image", 0], "filename_prefix": "comani-integration",
        }},
    }
    try:
        async with ClientSession() as http, http.ws_connect(url + "/ws") as ws:
            async with http.post(url + "/prompt", json={"prompt": prompt}) as response:
                response.raise_for_status()
                call_id = (await response.json())["prompt_id"]
            messages = []
            async with asyncio.timeout(10):
                while True:
                    message = await ws.receive_json()
                    messages.append(message)
                    if message.get("type") in {"completed", "failed"} and message.get("call_id") == call_id:
                        break
            async with http.get(url + "/history/" + call_id) as response:
                history = (await response.json())[call_id]
            assert history["status"]["completed"] is True
            image = history["outputs"]["save"]["images"][0]
            filename = tmp_path / image["subfolder"] / image["filename"]
            assert filename.is_file()
            with Image.open(filename) as saved:
                assert saved.size == (64, 64)
                assert json.loads(saved.info["prompt"])["save"]["class_type"] == "SaveImage"
            assert any(m.get("native_type") == "executed" and m.get("data", {}).get("node") == "save"
                       for m in messages)
    finally:
        folder_paths.set_output_directory(previous_output)


@pytest.mark.parametrize("native_http", ["integration-token"], indirect=True)
async def test_http_bearer_token(native_http):
    _, url = native_http
    async with Client(url, token="integration-token") as remote:
        call = remote.python(lambda: 23)
        assert await (await call.output()).fetch() == 23
    async with ClientSession() as http:
        async with http.post(url + "/rpc", json={"method": "handshake", "params": {}},
                             headers={"Authorization": "Bearer wrong-token"}) as response:
            assert response.status == 401
            assert (await response.json())["error"]["code"] == "Unauthorized"
    wrong = Client(url, token="wrong-token")
    try:
        with pytest.raises(RemoteError) as caught:
            await wrong.start()
        assert caught.value.details["code"] == "Unauthorized"
    finally:
        try:
            await wrong.close(wait=False)
        except RemoteError:
            pass
