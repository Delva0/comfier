"""Failed-call observation reports facts without changing execution outcomes."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from comani import Client, InProcessTransport, NotSubmitted, RemoteError, SessionClosed
from comani.daemon.adapter import NativeAdapter
from comani.daemon.objects import CallFailed
from comani.daemon.service import CallState, NativeRuntime, RecoveryError

from .conftest import FakeRuntime


@pytest.mark.parametrize("early_failure", [False, True])
def test_adapter_failure_observes_only_this_invocations_ui(tmp_path, early_failure):
    adapter = NativeAdapter(tmp_path)
    adapter.boot = lambda: None
    adapter.executor = SimpleNamespace(history_result={"outputs": {"previous": {"text": ["old"]}}},
                                       caches=SimpleNamespace(outputs=SimpleNamespace()))

    async def execute(*args):
        if early_failure:
            raise CallFailed("validation failed")
        adapter.executor.history_result = {"outputs": {"saved": {"images": [
            {"filename": "committed.png", "type": "output"}]}}}
        adapter.executor.success = False
        adapter.executor.status_messages = [("execution_error", {"exception_message": "later failed"})]

    adapter._execute = execute
    observations = []
    with pytest.raises(CallFailed):
        adapter.run({}, [], {"input": [object()]}, lambda *args: None,
                    prompt_id="current", observe=lambda value: observations.append(NativeRuntime._metadata(value)))
    assert observations[0]["prompt_id"] == "current"
    assert observations[0]["ui"] == ({} if early_failure else {"saved": {"images": [
        {"filename": "committed.png", "type": "output"}]}})
    assert adapter.bound == {} and adapter.capture is None


async def test_observation_is_scoped_to_live_session_and_does_not_return_refs():
    runtime = NativeRuntime.__new__(NativeRuntime)
    runtime.closing = False
    runtime.session_ttl = 3600
    runtime.session_seen = {"owner": time.monotonic(), "other": time.monotonic()}
    done = asyncio.get_running_loop().create_future()
    done.set_result({"$error": {"type": "CallFailed", "message": "failed later"}})
    state = CallState("owner", "failed", {}, done, status="failed",
                      error={"type": "CallFailed", "message": "failed later"},
                      metadata={"prompt_id": "failed", "executed": ["save", "fail"],
                                "ui": {"save": {"text": ["completed"]}}})
    runtime.calls = {("owner", "failed"): state}
    with pytest.raises(CallFailed, match="failed later"):
        await runtime.wait_result("owner", "failed")
    observed = await runtime.wait_result("owner", "failed", observation=True)
    assert observed == {"status": "failed", "error": state.error, "metadata": state.metadata}
    assert "outputs" not in observed
    observed["metadata"]["ui"].clear()
    assert state.metadata["ui"]
    with pytest.raises(RecoveryError) as hidden:
        await runtime.wait_result("other", "failed", observation=True)
    assert hidden.value.code == "CallExpired"
    runtime.calls.clear()
    with pytest.raises(RecoveryError) as gone:
        await runtime.wait_result("owner", "failed", observation=True)
    assert gone.value.code == "CallExpired"
    runtime.session_seen.pop("owner")
    with pytest.raises(SessionClosed):
        await runtime.wait_result("owner", "failed", observation=True)


async def test_capture_observation_is_not_submitted(peer):
    remote, _ = peer
    with remote.graph():
        call = remote.node("Echo", return_call=True, value=1)
        for method in (call.metadata, call.files, call.file):
            with pytest.raises(NotSubmitted):
                await method(allow_failed=True)


async def test_local_upload_failure_is_not_fabricated_as_remote_metadata():
    runtime = FakeRuntime()

    async def rejected(**params):
        raise RemoteError("upload rejected before admission")

    runtime.artifact_put = rejected
    async with Client(transport=InProcessTransport(runtime)) as remote:
        call = remote.python(lambda: 1, return_call=True)
        with pytest.raises(RemoteError, match="before admission"):
            await call.metadata(allow_failed=True)
        assert not runtime.submissions


async def test_old_runtime_failure_is_explicit_and_success_stays_compatible(peer):
    remote, _ = peer
    success = remote.node("Echo", return_call=True, value=1)
    assert (await success.metadata(allow_failed=True)).status == "succeeded"
    failed = remote.node("Fail", return_call=True)
    with pytest.raises(RemoteError, match="does not support"):
        await failed.metadata(allow_failed=True)
    with pytest.raises(RemoteError, match="producer failed"):
        await failed
    with pytest.raises(TypeError, match="bool"):
        await success.metadata(allow_failed=1)
