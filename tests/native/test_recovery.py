"""Recovery contracts exercised against the actual native CPU executor."""

import asyncio
import os
import threading
import time

import pytest

from comani.daemon import NativeRuntime
from comani.daemon.objects import RefExpired, SessionClosed
from comani.daemon.service import RecoveryError, RecoveryPolicy

COMFY = os.environ.get("COMANI_COMFY_ROOT", "/home/delva/projects/ComfyUI")
pytestmark = pytest.mark.skipif(not os.path.isfile(COMFY + "/execution.py"), reason="real ComfyUI required")


class Echo:
    RETURN_TYPES = ("INT",)
    FUNCTION = "run"

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("INT",)}}

    def run(self, value):
        return (value,)


def spec(value=1, node="RecoveryEcho"):
    return {"kind": "node", "node_type": node, "inputs": {"value": value}}


async def test_resume_digest_forget_and_expired_window_never_repeat_effect():
    effects = []

    class Effect(Echo):
        def run(self, value):
            effects.append(value)
            return (value,)

    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False,
                                  recovery_policy=RecoveryPolicy(submission_seconds=0.1)).start()
    runtime.adapter.nodes.NODE_CLASS_MAPPINGS["RecoveryEffect"] = Effect
    handshake = await runtime.handshake()
    session, window = handshake["session_id"], handshake["submission_window"]
    request = spec(42, "RecoveryEffect")
    try:
        replies = await asyncio.gather(*(runtime.submit(session, "one-effect", request,
                                                       submission_window=window) for _ in range(20)))
        assert {reply["call_id"] for reply in replies} == {"one-effect"}
        result = await runtime.wait_result(session, "one-effect")
        assert effects == [42]
        resumed = await runtime.resume(session, handshake["resume_token"], handshake["epoch"])
        assert resumed["session_id"] == session
        ref = result["outputs"][0]
        assert await runtime.fetch_bytes(session, ref["id"], ref["epoch"])
        with pytest.raises(SessionClosed, match="identity"):
            await runtime.resume(session, "wrong", handshake["epoch"])
        with pytest.raises(RefExpired, match="restarted"):
            await runtime.resume(session, handshake["resume_token"], "another-epoch")
        with pytest.raises(RecoveryError) as conflict:
            await runtime.submit(session, "one-effect", spec(43, "RecoveryEffect"), submission_window=window)
        assert conflict.value.code == "SubmissionKeyConflict"
        await runtime.forget_calls(session, ["one-effect"], runtime.epoch)
        assert (session, "one-effect") not in runtime.calls
        with pytest.raises(RecoveryError) as forgotten:
            await runtime.submit(session, "one-effect", request, submission_window=window)
        assert forgotten.value.code == "CallExpired"
        await asyncio.sleep(0.12)
        with pytest.raises(RecoveryError) as expired:
            await runtime.submit(session, "one-effect", request, submission_window=window)
        assert expired.value.code == "CallExpired"
        assert effects == [42] and not runtime.submissions
    finally:
        await runtime.close()


async def test_expiry_discards_unstarted_work_and_finishes_native_without_other_session_interrupt():
    started, finish = threading.Event(), threading.Event()
    effects = []

    class Blocked(Echo):
        def run(self, value):
            started.set()
            assert finish.wait(10)
            effects.append(value)
            return (value,)

    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False, session_ttl=2).start()
    runtime.adapter.nodes.NODE_CLASS_MAPPINGS.update(RecoveryEcho=Echo, RecoveryBlocked=Blocked)
    first, other = await runtime.handshake(), await runtime.handshake()
    session = first["session_id"]
    try:
        await runtime.submit(session, "running", spec(1, "RecoveryBlocked"))
        async with asyncio.timeout(3):
            while not started.is_set():
                await asyncio.sleep(0.005)
        await runtime.submit(session, "unstarted", spec(2, "RecoveryBlocked"))
        dependent = {"$ref": {"session": session, "epoch": runtime.epoch,
                               "call_id": "running", "slot": 0}}
        await runtime.submit(session, "dependency-wait", spec(dependent, "RecoveryBlocked"))
        await runtime.submit(other["session_id"], "other", spec(9))
        await asyncio.sleep(0)
        assert runtime.calls[(session, "running")].status == "running"
        assert runtime.calls[(session, "unstarted")].status == "queued"
        assert runtime.calls[(session, "dependency-wait")].status == "accepted"
        runtime.session_seen[session] = time.monotonic() - 3
        with pytest.raises(SessionClosed, match="grace"):
            await runtime.resume(session, first["resume_token"], runtime.epoch)
        await asyncio.sleep(0)
        assert session not in runtime.session_seen
        assert runtime.active is not None and runtime.active.session == session
        assert not finish.is_set()
        finish.set()
        result = await runtime.wait_result(other["session_id"], "other")
        await runtime.queue.join()
        assert result["outputs"] and effects == [1]
        assert not any(entry.session == session for entry in runtime.store.entries.values())
        assert not any(owner == session for owner, _ in runtime.calls)
    finally:
        finish.set()
        await runtime.close()


async def test_call_and_object_recovery_capacity_rejects_without_silent_eviction():
    runtime = await NativeRuntime(COMFY, cpu=True, custom_nodes=False,
        recovery_policy=RecoveryPolicy(max_sessions=1, max_call_records=1, max_objects=1)).start()
    runtime.adapter.nodes.NODE_CLASS_MAPPINGS["RecoveryEcho"] = Echo
    handshake = await runtime.handshake()
    session = handshake["session_id"]
    try:
        with pytest.raises(RecoveryError) as capacity:
            await runtime.handshake()
        assert capacity.value.code == "RecoveryCapacity"
        await runtime.submit(session, "first", spec())
        result = await runtime.wait_result(session, "first")
        with pytest.raises(RecoveryError) as capacity:
            await runtime.submit(session, "second", spec(2))
        assert capacity.value.code == "RecoveryCapacity"
        assert await runtime.wait_result(session, "first") == result
        runtime.store._capacity(0)
        with pytest.raises(Exception, match="object recovery capacity"):
            runtime.store.own(session, [7])
        await runtime.close_session(session)
        assert not runtime.store.entries and not runtime.submissions
        assert (await runtime.handshake())["session_id"] != session
    finally:
        await runtime.close()
