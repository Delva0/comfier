"""A fresh management connection learns graph arity from its real runtime."""

import asyncio

import pytest

from comani import (
    Client,
    CodeCompatibilityError,
    Expired,
    GraphRef,
    InProcessTransport,
    RemoteError,
    UnknownOutputLayout,
)

from .conftest import FakeRuntime


class RuntimeStoppedError(RemoteError):
    code = "RuntimeStopped"


class ManagedSchemaRuntime(FakeRuntime):
    def __init__(self, *, stopped=False, incompatible=False):
        super().__init__()
        self.stopped, self.incompatible = stopped, incompatible
        self.compute_handshakes = 0
        self.attaching, self.attach_gate = asyncio.Event(), asyncio.Event()
        self.attach_gate.set()

    async def management_request(self, method, **params):
        assert method == "handshake"  # No install/start or other management mutation.
        return {"daemon_epoch": "management-only", "session_id": "management-session",
                "python": [0, 0, 0], "cloudpickle": "management-version",
                "nodes": {"Pair": {"outputs": []}}, "capabilities": {"management": 1}}

    async def handshake(self, session_id):
        self.compute_handshakes += 1
        self.attaching.set()
        await self.attach_gate.wait()
        if self.stopped:
            raise RuntimeStoppedError("The runtime is stopped")
        result = await super().handshake(session_id)
        result["nodes"]["Effect"]["output_node"] = True
        result["nodes"]["Dynamic"] = {}
        if self.incompatible:
            result.update(python=[0, 0, 0], cloudpickle="runtime-version")
        return result


async def test_first_graph_await_attaches_once_and_never_executes_capture():
    runtime = ManagedSchemaRuntime()
    runtime.attach_gate.clear()
    async with Client(transport=InProcessTransport(runtime)) as remote:
        assert not remote.epoch and runtime.compute_handshakes == 0
        with remote.graph() as graph:
            pair = asyncio.ensure_future(remote.node("Pair", return_call=True, value=1))
            echo = asyncio.ensure_future(remote.node("Echo", return_call=True, value=2))
            await runtime.attaching.wait()
            await asyncio.sleep(0)
            assert runtime.compute_handshakes == 1 and not runtime.submissions
            runtime.attach_gate.set()
            pair_refs, echo_refs = await asyncio.gather(pair, echo)
            assert len(pair_refs) == 2 and len(echo_refs) == 1
            assert all(isinstance(ref, GraphRef) for ref in (*pair_refs, *echo_refs))
            assert await remote.node("Effect", return_call=True, value=3) == ()
            assert graph._automatic_targets == ["3"]
            graph.output("result", echo_refs[0])
        assert remote.epoch == runtime.epoch and not runtime.submissions and not runtime.effects
        await remote.submit(graph)
        assert len(runtime.submissions) == 1


async def test_stopped_runtime_error_is_not_relabelled_as_unknown_arity():
    runtime = ManagedSchemaRuntime(stopped=True)
    async with Client(transport=InProcessTransport(runtime)) as remote:
        with pytest.raises(RemoteError, match="stopped") as failure:
            with remote.graph():
                await remote.node("Echo", return_call=True, value=1)
        assert failure.value.details["code"] == "RuntimeStopped"
        assert runtime.compute_handshakes == 1 and not runtime.submissions


async def test_unknown_layout_remains_explicit_after_runtime_schema_is_available():
    runtime = ManagedSchemaRuntime()
    async with Client(transport=InProcessTransport(runtime)) as remote:
        with pytest.raises(UnknownOutputLayout):
            with remote.graph():
                await remote.node("Dynamic", return_call=True)
        assert remote.epoch == runtime.epoch
        assert runtime.compute_handshakes == 1 and not runtime.submissions


async def test_python_capture_defers_runtime_attachment_and_callable_abi_check_until_submit():
    runtime = ManagedSchemaRuntime(incompatible=True)
    async with Client(transport=InProcessTransport(runtime)) as remote:
        with remote.graph() as graph:
            port, = await remote.python(lambda: 42)
            graph.output("result", port)
        assert runtime.compute_handshakes == 0 and not remote.epoch
        with pytest.raises(CodeCompatibilityError):
            await remote.submit(graph)
        assert runtime.compute_handshakes == 1 and not runtime.submissions
        assert remote.handshake["cloudpickle"] == "runtime-version"


@pytest.mark.parametrize("artifact_kind", ["callable", "source"])
async def test_known_python_output_layout_captures_offline_without_transport(artifact_kind, tmp_path):
    remote = Client("http://127.0.0.1:1")
    with remote.graph() as graph:
        if artifact_kind == "callable":
            port, = await remote.python(lambda: {"items": [1, 2]})
        else:
            source = tmp_path / "produce.py"
            source.write_text("def produce():\n    return {'items': [1, 2]}\n")
            port, = await remote.python(source=source, entry="produce")
        graph.output("value", port)
    assert isinstance(port, GraphRef) and graph.sealed and len(graph.nodes) == 1
    assert remote._opening is None and remote._compute_opening is None
    assert remote._queue.empty() and not remote._states
    await remote.close()


async def test_cancelling_graph_waiter_does_not_cancel_shared_runtime_attachment():
    runtime = ManagedSchemaRuntime()
    runtime.attach_gate.clear()
    async with Client(transport=InProcessTransport(runtime)) as remote:
        with remote.graph() as graph:
            first = asyncio.ensure_future(remote.node("Echo", return_call=True, value=1))
            second = asyncio.ensure_future(remote.node("Echo", return_call=True, value=2))
            await runtime.attaching.wait()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not remote._compute_opening.cancelled()
            runtime.attach_gate.set()
            port, = await second
            graph.output("result", port)
        assert runtime.compute_handshakes == 1 and not runtime.submissions


@pytest.mark.parametrize("retry_entry", ["graph", "eager"])
async def test_failed_attachment_retries_after_another_client_starts_runtime(retry_entry):
    runtime = ManagedSchemaRuntime(stopped=True)
    async with Client(transport=InProcessTransport(runtime)) as remote:
        with pytest.raises(RemoteError, match="stopped"):
            with remote.graph():
                await remote.node("Echo", return_call=True, value=1)
        assert runtime.compute_handshakes == 1 and not remote.epoch
        # The remote process is now available, without a lifecycle call on this Client.
        runtime.stopped = False
        if retry_entry == "graph":
            with remote.graph() as graph:
                port, = await remote.node("Echo", return_call=True, value=2)
                graph.output("result", port)
            assert not runtime.submissions
            await remote.submit(graph)
        else:
            ref, = await remote.node("Echo", return_call=True, value=2)
            assert await ref.fetch() == 2
        assert runtime.compute_handshakes == 2 and len(runtime.submissions) == 1


async def test_retried_attachment_is_singleflight_and_waiter_cancellation_is_local():
    runtime = ManagedSchemaRuntime(stopped=True)
    async with Client(transport=InProcessTransport(runtime)) as remote:
        with pytest.raises(RemoteError, match="stopped"):
            with remote.graph():
                await remote.node("Echo", return_call=True, value=0)
        runtime.stopped = False
        runtime.attaching.clear()
        runtime.attach_gate.clear()
        with remote.graph() as graph:
            waiters = [asyncio.ensure_future(remote.node("Echo", return_call=True, value=value)) for value in range(24)]
            await runtime.attaching.wait()
            await asyncio.sleep(0)
            assert runtime.compute_handshakes == 2 and not runtime.submissions
            waiters[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiters[0]
            assert not remote._compute_opening.cancelled()
            runtime.attach_gate.set()
            ready = await asyncio.gather(*waiters[1:])
            assert len(ready) == 23
            graph.output("result", ready[-1][0])
        assert runtime.compute_handshakes == 2 and not runtime.submissions


async def test_expired_successful_session_does_not_create_a_replacement_attachment():
    runtime = ManagedSchemaRuntime()
    async with Client(transport=InProcessTransport(runtime)) as remote:
        old_ref, = await remote.node("Echo", return_call=True, value="original-session")
        opening = remote._compute_opening
        remote._expire_compute(Expired("Runtime epoch changed"))
        remote._heartbeat_error = Expired("Runtime epoch changed")
        with pytest.raises(Expired):
            with remote.graph():
                await remote.node("UnseenNative", return_call=True, value="must-not-replay")
        with pytest.raises(Expired):
            await old_ref.fetch()
        assert remote._compute_opening is opening
        assert runtime.compute_handshakes == 1 and len(runtime.submissions) == 1
