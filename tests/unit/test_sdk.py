import asyncio
import gc
import inspect
import io
import os
import weakref
import zipfile

import pytest

from comani import (
    Client,
    CodeCompatibilityError,
    GraphRef,
    InProcessTransport,
    InvalidReference,
    NotSubmitted,
    Released,
    RemoteError,
    SessionClosed,
    UnknownOutputLayout,
)
from comani.code import prepare_code
from comani.protocol import decode_tree, encode_tree, lift_arguments, restore_arguments

from ._helpers import eventually


async def test_unawaited_calls_execute_and_original_names_survive(peer):
    client, runtime = peer
    call = client.node("Effect", return_call=True, **{"任意 输入名": 42})
    await eventually(lambda: bool(runtime.effects))
    assert runtime.effects == [{"任意 输入名": 42}]
    assert await call == ()
    ref, = await client.node("原始 节点-name", return_call=True, **{"non-identifier key": "yes"})
    assert await ref.fetch() == {"non-identifier key": "yes"}


async def test_pending_dependency_does_not_block_unrelated_dispatch(peer):
    client, runtime = peer
    a = client.node("Block", return_call=True, value="A")
    c = client.node("Echo", return_call=True, value=a.output(0))
    b = client.node("Echo", return_call=True, value="B")
    ref, = await b
    assert await ref.fetch() == "B"
    assert len(runtime.submissions) == 3 and c.status == "accepted"
    runtime.gate.set()
    ref, = await c
    assert await ref.fetch() == "A"


async def test_dependency_failure_does_not_hang(peer):
    client, _ = peer
    failed = client.node("Fail", return_call=True)
    consumer = client.node("Echo", return_call=True, value=failed.output(0))
    with pytest.raises(RemoteError, match="producer failed"):
        await asyncio.wait_for(consumer, timeout=1)


async def test_ready_ref_is_not_awaitable_and_literal_link_remains_data(peer):
    client, _ = peer
    call = client.node("Echo", return_call=True, value=["node-id", 0])
    future = call.output(0)
    ref, = await call
    assert await future is ref
    assert not inspect.isawaitable(ref)
    assert await ref.fetch() == ["node-id", 0]
    with pytest.raises(TypeError):
        bool(ref)


async def test_call_future_aliases_share_gc_owner(peer):
    client, runtime = peer
    call = client.node("Echo", return_call=True, value=123)
    future = call.output(0)
    ref, = await call
    alias = await future
    key, weak_cell = ref.id, weakref.ref(ref._cell)
    del ref, alias, future
    gc.collect()
    assert weak_cell() is not None and key not in runtime.released
    del call
    gc.collect()
    await eventually(lambda: key in runtime.released)
    assert runtime.released.count(key) == 1


async def test_dropped_pending_call_releases_unclaimed_outputs(peer):
    client, runtime = peer
    call = client.node("Block", return_call=True, value="unused")
    del call
    gc.collect()
    assert not runtime.forgotten
    runtime.gate.set()
    await eventually(lambda: bool(runtime.released))
    assert len(runtime.released) == 1


async def test_explicit_release_invalidates_all_aliases(peer):
    client, runtime = peer
    call = client.node("Echo", return_call=True, value=42)
    ref, = await call
    ref.release()
    with pytest.raises(Released):
        await call.output(0)
    await eventually(lambda: len(runtime.released) == 1)


async def test_cross_session_inputs_are_rejected(peer):
    client, runtime = peer
    ref, = await client.node("Echo", return_call=True, value=1)
    async with Client(transport=InProcessTransport(runtime)) as other:
        with pytest.raises(InvalidReference):
            other.node("Echo", return_call=True, value=ref)


async def test_capture_is_symbolic_run_bound_and_borrowed(peer):
    client, runtime = peer
    with client.graph() as graph:
        source = graph.input("image", type="IMAGE")
        call = client.node("Echo", return_call=True, value=source)
        output, = await call
        assert isinstance(output, GraphRef) and await output is output
        graph.output("named result", output)
        graph.target(call)
        assert not runtime.submissions
    with pytest.raises(NotSubmitted):
        await call
    with pytest.raises(NotSubmitted):
        client.node("Echo", return_call=True, value=output)
    run = client.submit(graph, inputs={"image": ["looks-like-link", 0]}, return_call=True)
    result, = await run
    middle = await run[output]
    assert result.ownership == middle.ownership == "borrowed"
    assert await middle.fetch() == "middle:1"
    spec = runtime.submissions[0][1]
    assert spec["bindings"]["image"] == {"$list": ["looks-like-link", 0]}
    assert spec["graph"]["nodes"]["1"]["inputs"]["value"] == {"$input": "image"}
    del result, middle, run
    gc.collect()
    await asyncio.sleep(0)
    assert not runtime.released


async def test_workflow_composition_remaps_internal_ports(peer):
    client, runtime = peer
    with client.graph() as inner:
        value = inner.input("x")
        out, = await client.node("Echo", return_call=True, value=value)
        inner.output("out", out)
    with client.graph() as outer:
        value = outer.input("source")
        first = client.submit(inner, inputs={"x": value}, return_call=True)
        second = client.submit(inner, inputs={"x": first.output("out")}, return_call=True)
        outer.output("result", second.output("out"))
    await client.submit(outer, inputs={"source": 4})
    nodes = runtime.submissions[0][1]["graph"]["nodes"]
    assert set(nodes) == {"1", "2"}
    assert nodes["2"]["inputs"]["value"] == {"$graph_ref": {"node_id": "1", "slot": 0}}


async def test_failed_capture_restores_live_receiver(peer):
    client, _ = peer
    with pytest.raises(RuntimeError):
        with client.graph() as graph:
            raise RuntimeError("stop")
    with pytest.raises(ValueError):
        client.submit(graph)
    ref, = await client.node("Echo", return_call=True, value="live")
    assert await ref.fetch() == "live"


async def test_unknown_layout_has_explicit_slot_escape(peer):
    client, _ = peer
    with client.graph() as graph:
        call = client.node("UnknownSchema", return_call=True)
        with pytest.raises(UnknownOutputLayout):
            await call
        graph.output("out", call.output(4))


async def test_explicit_pending_slots_allow_runtime_layout_to_grow(peer):
    client, _ = peer
    client._nodes["Pair"]["outputs"] = ["*"]
    call = client.node("Pair", return_call=True, declared_outputs=False, value="dynamic")
    later_slot = call.output(1)
    ref = await later_slot
    assert await ref.fetch() == "dynamic"
    assert len(await call) == 2
    with client.graph() as graph:
        captured = client.node("Pair", return_call=True, value=1)
        graph.output("explicit slot", captured.output(1))


async def test_python_nested_refs_tuple_and_artifact_reuse(peer):
    client, runtime = peer
    seed = client.node("Echo", return_call=True, value=3)
    def function(tree):
        return tree["a"][0] + 1, False, None
    result, = await client.python(function, inputs={"tree": {"a": [seed.output(0)]}}, return_call=True)
    assert await result.fetch() == (4, False, None)
    await client.python(function, inputs={"tree": {"a": [3]}}, return_call=True)
    assert len(runtime.artifacts) == 1


async def test_graph_python_uploads_at_submit_and_lifts_ports(peer):
    client, runtime = peer
    with client.graph() as graph:
        source = graph.input("x")
        value, = await client.python(lambda tree: tree[0], inputs={"tree": [source]}, return_call=True)
        graph.output("value", value)
    assert not runtime.artifacts
    await client.submit(graph, inputs={"x": 1})
    node = runtime.submissions[0][1]["graph"]["nodes"]["1"]
    assert node["class_type"] == "ComaniPython"
    assert node["inputs"]["v0"] == {"$input": "x"}
    assert runtime.submissions[0][1]["graph"]["targets"] == ["1"]


async def test_pickle_incompatibility_has_no_source_fallback(peer):
    client, runtime = peer
    client.handshake["python"] = [0, 0, 0]
    with pytest.raises(CodeCompatibilityError):
        await client.python(lambda: 1, return_call=True)
    assert not runtime.artifacts and not runtime.submissions


def test_package_content_id_and_environment_exclusions(tmp_path):
    package = tmp_path / "ops"
    package.mkdir()
    (package / "__init__.py").write_text("def main(): return 1\n")
    (package / ".env").write_text("DO_NOT_PACKAGE=1")
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "x.pyc").write_bytes(b"cache")
    a = prepare_code(source=package, entry="ops:main")
    os.utime(package / "__init__.py", (1234567890, 1234567890))
    os.chmod(package / "__init__.py", 0o600)
    assert a.artifact_id == prepare_code(source=package, entry="ops:main").artifact_id
    assert a.descriptor()["python"] is None
    assert a.descriptor()["package_root"] == "ops"
    with zipfile.ZipFile(io.BytesIO(a.data)) as archive:
        assert archive.namelist() == ["ops/__init__.py"]


def test_package_short_entry_is_normalized_without_rewriting_source(tmp_path):
    package = tmp_path / "normal_package"
    package.mkdir()
    source = "from normal_package.helper import answer\ndef main(): return answer\n"
    (package / "pipeline.py").write_text(source)
    (package / "helper.py").write_text("answer = 42\n")
    artifact = prepare_code(source=package, entry="pipeline:main")
    assert artifact.entrypoint == "normal_package.pipeline:main"
    assert artifact.package_root == "normal_package"
    assert artifact.artifact_id == prepare_code(source=package, entry="normal_package.pipeline:main").artifact_id
    with zipfile.ZipFile(io.BytesIO(artifact.data)) as archive:
        assert archive.read("normal_package/pipeline.py").decode() == source


async def test_auto_output_roots_keep_effects_without_forcing_every_node(peer):
    client, runtime = peer
    client._nodes["Effect"]["output_node"] = True
    with client.graph() as graph:
        client.node("Echo", return_call=True, value="unused")
        client.node("Effect", return_call=True, value="root")
    await client.submit(graph, return_call=True)
    assert runtime.submissions[0][1]["graph"]["targets"] == ["2"]


def test_tree_and_python_layout_preserve_list_tuple_and_literal_tags():
    value = {"x": ["12", 0], "tuple": (1, {"$ref": "literal"})}
    assert decode_tree(encode_tree(value)) == value
    layout, ports = lift_arguments((value,), {"option": False})
    assert restore_arguments(layout, ports) == ((value,), {"option": False})


async def test_intermediate_wait_waits_ack_not_whole_workflow(peer):
    client, runtime = peer
    gate = asyncio.Event()
    original_upload = runtime.artifact_put

    async def upload(**params):
        await gate.wait()
        return await original_upload(**params)

    runtime.artifact_put = upload
    with client.graph() as graph:
        port, = await client.python(lambda: 1, return_call=True)
        graph.output("out", port)
    run = client.submit(graph, return_call=True)
    waiter = asyncio.ensure_future(run[port])
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not runtime.submissions and not waiter.done()
    gate.set()
    ref = await asyncio.wait_for(waiter, timeout=1)
    assert await ref.fetch() == "middle:1"


async def test_pending_inputs_are_held_until_daemon_accepts_them(peer):
    client, runtime = peer
    value, = await client.node("Echo", return_call=True, value=42)
    key, cell = value.id, weakref.ref(value._cell)
    entered, gate = asyncio.Event(), asyncio.Event()
    original_submit = runtime.submit

    async def submit(**params):
        entered.set()
        await gate.wait()
        return await original_submit(**params)

    runtime.submit = submit
    consumer = client.node("Echo", return_call=True, value=value)
    del value
    await entered.wait()
    gc.collect()
    assert cell() is not None and key not in runtime.released
    gate.set()
    await consumer
    gc.collect()
    await eventually(lambda: key in runtime.released)


async def test_callable_captured_reference_is_rejected_with_inputs_hint(peer):
    client, _ = peer
    value, = await client.node("Echo", return_call=True, value=2)

    def accidentally_captured():
        return value

    with pytest.raises(TypeError, match="inputs"):
        client.python(accidentally_captured, return_call=True)


async def test_canceling_one_waiter_does_not_cancel_remote_call(peer):
    client, runtime = peer
    call = client.node("Block", return_call=True, value="still running")
    waiter = asyncio.ensure_future(call)
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    runtime.gate.set()
    result, = await call
    assert await result.fetch() == "still running"


async def test_close_without_wait_fails_queued_calls_and_drops_input_queue(peer):
    client, runtime = peer
    call = client.node("Block", return_call=True, value=3)
    await eventually(lambda: bool(runtime.submissions))
    await client.close(wait=False)
    with pytest.raises(SessionClosed):
        await call
    assert client._queue.empty()
    runtime.gate.set()


async def test_heartbeat_continues_during_a_long_call(peer):
    _, runtime = peer
    original_handshake = runtime.handshake
    heartbeats = []

    async def handshake(**params):
        payload = await original_handshake(**params)
        payload["session_ttl"] = 0.03
        return payload

    async def heartbeat(**params):
        heartbeats.append(params)
        return {"alive": True}

    runtime.handshake, runtime.heartbeat = handshake, heartbeat
    async with Client(transport=InProcessTransport(runtime)) as client:
        call = client.node("Block", return_call=True, value=1)
        await eventually(lambda: len(heartbeats) >= 2)
        assert call.status == "accepted"
        runtime.gate.set()
        await call
    count = len(heartbeats)
    await asyncio.sleep(0.03)
    assert len(heartbeats) == count


async def test_one_retained_slot_does_not_keep_other_owned_slots(peer):
    client, runtime = peer
    future = client.node("Pair", return_call=True, value="both").output(1)
    ref = await future
    assert await ref.fetch() == "both"
    await eventually(lambda: len(runtime.released) == 1)
    assert ref.id not in runtime.released
    state = weakref.ref(ref._cell.state)
    call_id = ref._cell.state.call_id
    assert call_id not in runtime.forgotten
    del future, ref
    gc.collect()
    await eventually(lambda: len(runtime.released) == 2)
    gc.collect()
    assert state() is None
    await eventually(lambda: call_id in runtime.forgotten)


async def test_failed_call_does_not_become_a_hidden_client_root(peer):
    client, _ = peer

    async def fail_and_drop():
        call = client.node("Fail", return_call=True)
        state = weakref.ref(call._state)
        try:
            await call
        except RemoteError:
            pass
        return state

    state = await fail_and_drop()
    await asyncio.sleep(0)
    gc.collect()
    assert state() is None


async def test_middle_borrow_keeps_invocation_metadata_until_last_reference(peer):
    client, runtime = peer
    with client.graph() as graph:
        middle, = await client.node("Echo", return_call=True, value=3)
        graph.output("out", middle)
    run = client.submit(graph, return_call=True)
    call_id = run.id
    borrowed = await run[middle]
    await run
    del run
    gc.collect()
    await asyncio.sleep(0)
    assert call_id not in runtime.forgotten
    assert await borrowed.fetch() == "middle:1"
    del borrowed
    gc.collect()
    await eventually(lambda: call_id in runtime.forgotten)
