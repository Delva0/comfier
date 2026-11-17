"""Ordered transport framing preserves per-call acceptance and recovery."""

import asyncio

import pytest

from comfier import Client, RemoteError, SessionClosed
from comfier.daemon.service import NativeRuntime, RecoveryPolicy

from .test_sdk_recovery import DroppingTransport, RecoverableRuntime


class BatchRuntime(RecoverableRuntime):
    submit_batch = NativeRuntime.submit_batch

    def __init__(self):
        super().__init__()
        self.batches = []

    async def handshake(self, session_id):
        value = await super().handshake(session_id)
        value["capabilities"]["submit_batch"] = 1
        value["nodes"]["Reject"] = {"outputs": ["*"]}
        return value

    async def start(self):
        return self

    def _session(self, session_id):
        pass

    async def submit(self, session_id, call_id, spec, submission_window=None):
        if spec.get("node_type") == "Reject":
            raise ValueError("one rejected admission")
        accepted = await super().submit(session_id, call_id, spec, submission_window)
        return {**accepted, "call_id": call_id}


class TraceTransport(DroppingTransport):
    def __init__(self, runtime):
        super().__init__(runtime)
        self.methods, self.envelopes = [], []
        self.drop_prefix = False

    async def request(self, method, **params):
        self.methods.append(method)
        if method == "submit_batch":
            self.envelopes.append(params)
            if self.drop_prefix:
                self.drop_prefix = False
                for item in params["submissions"][:2]:
                    await self.runtime.submit(session_id=params["session_id"], **item)
                raise ConnectionError("ACK lost after an accepted prefix")
        return await super().request(method, **params)


async def test_already_queued_future_chain_is_one_envelope_with_unchanged_values():
    runtime = BatchRuntime()
    transport = TraceTransport(runtime)
    async with Client(transport=transport) as remote:
        value = 41
        calls = []
        for _ in range(12):
            call = remote.node("Echo", value=value, return_call=True)
            calls.append(call)
            value = call.output(0)
        assert await (await value).fetch() == 41
        assert transport.methods.count("submit_batch") == 1
        assert "submit" not in transport.methods
        assert [item["call_id"] for item in transport.envelopes[0]["submissions"]] == [c.id for c in calls]
        assert all([(await c.output(0)).ownership == "owned" for c in calls])


async def test_bad_item_does_not_cancel_later_independent_calls():
    runtime = BatchRuntime()
    async with Client(transport=TraceTransport(runtime)) as remote:
        first = remote.node("Echo", value="first")
        rejected = remote.node("Reject", value=0)
        last = remote.node("Echo", value="last")
        assert await (await first).fetch() == "first"
        with pytest.raises(RemoteError, match="rejected"):
            await rejected
        assert await (await last).fetch() == "last"


async def test_local_non_json_item_does_not_cancel_its_neighbours():
    runtime = BatchRuntime()
    async with Client(transport=TraceTransport(runtime)) as remote:
        first = remote.node("Echo", value=1)
        invalid = remote.node("Echo", value=float("nan"))
        last = remote.node("Echo", value=3)
        assert await (await first).fetch() == 1
        with pytest.raises(ValueError):
            await invalid
        assert await (await last).fetch() == 3


@pytest.mark.parametrize("prefix_only", [False, True])
async def test_lost_ack_replays_identical_ids_specs_windows_without_repeating_effects(prefix_only):
    runtime = BatchRuntime()
    transport = TraceTransport(runtime)
    if prefix_only:
        transport.drop_prefix = True
    else:
        transport.drop_once.add("submit_batch")
    async with Client(transport=transport) as remote:
        calls = [remote.node("Effect", value=index, return_call=True) for index in range(5)]
        await asyncio.gather(*(asyncio.ensure_future(call) for call in calls))
        assert runtime.effects == [{"value": index} for index in range(5)]
        assert len(runtime.submissions) == 5
        assert transport.envelopes[0] == transport.envelopes[1]
        assert runtime.resumes == 1


async def test_old_daemon_uses_original_single_submit_path():
    runtime = RecoverableRuntime()
    transport = TraceTransport(runtime)
    async with Client(transport=transport) as remote:
        values = [remote.node("Echo", value=index) for index in range(5)]
        assert [await (await value).fetch() for value in values] == list(range(5))
        assert transport.methods.count("submit") == 5
        assert "submit_batch" not in transport.methods


async def test_batch_count_limit_chunks_only_the_envelope():
    runtime = BatchRuntime()
    transport = TraceTransport(runtime)
    async with Client(transport=transport) as remote:
        values = [remote.node("Echo", value=index) for index in range(70)]
        assert [await (await value).fetch() for value in values] == list(range(70))
        assert [len(item["submissions"]) for item in transport.envelopes] == [64, 6]


class AdmissionRuntime(NativeRuntime):
    """Real native admission/leases/windows, without starting the compute worker."""

    def __init__(self, **kwargs):
        super().__init__("/unused", **kwargs)
        self.adapter.node_types = lambda: {}

    async def start(self):
        self.loop = asyncio.get_running_loop()
        return self


def node(value):
    return {"kind": "node", "node_type": "Echo", "inputs": {"value": value}}


async def test_real_admission_conflict_and_missing_parent_do_not_stop_later_items():
    runtime = AdmissionRuntime()
    try:
        handshake = await runtime.handshake()
        session, window = handshake["session_id"], handshake["submission_window"]
        items = [{"call_id": "first", "spec": node(1), "submission_window": window},
                 {"call_id": "first", "spec": node(2), "submission_window": window},
                 {"call_id": "bad-parent", "spec": node({"$ref": {"call_id": "unknown", "slot": 0,
                    "session": session, "epoch": runtime.epoch}}), "submission_window": window},
                 {"call_id": "last", "spec": node(3), "submission_window": window}]
        result = await runtime.submit_batch(session, items)
        assert result["results"][1]["error"]["code"] == "SubmissionKeyConflict"
        assert result["results"][2]["error"]["code"] == "Expired"
        assert set(runtime.calls) == {(session, "first"), (session, "last")}
    finally:
        await runtime.close()


async def test_real_admission_strict_expiry_still_allows_existing_id_replay():
    runtime = AdmissionRuntime(recovery_policy=RecoveryPolicy(submission_seconds=0.03))
    try:
        handshake = await runtime.handshake()
        session, window = handshake["session_id"], handshake["submission_window"]
        item = {"call_id": "accepted", "spec": node(1), "submission_window": window}
        await runtime.submit_batch(session, [item])
        await asyncio.sleep(0.04)
        reply = await runtime.submit_batch(session, [item, {**item, "call_id": "new"}])
        assert reply["results"][0]["call_id"] == "accepted" and "error" not in reply["results"][0]
        assert reply["results"][1]["error"]["code"] == "CallExpired"
        assert (session, "new") not in runtime.calls
    finally:
        await runtime.close()


@pytest.mark.parametrize("version", [True, 2, "1"])
async def test_only_exact_integer_capability_enables_batching(version):
    runtime = BatchRuntime()
    original = runtime.handshake
    async def handshake(session_id):
        value = await original(session_id)
        value["capabilities"]["submit_batch"] = version
        return value
    runtime.handshake = handshake
    transport = TraceTransport(runtime)
    async with Client(transport=transport) as remote:
        values = [remote.node("Echo", value=index) for index in range(3)]
        assert [await (await value).fetch() for value in values] == [0, 1, 2]
        assert "submit_batch" not in transport.methods


async def test_node_python_and_workflow_keep_their_own_result_modes_across_ready_prefixes():
    runtime = BatchRuntime()
    transport = TraceTransport(runtime)
    async with Client(transport=transport) as remote:
        first = remote.node("Echo", value=41)
        lifted = remote.python(lambda value: value + 1, inputs={"value": first})
        graph = remote.graph({"a": {"class_type": "Echo", "inputs": {"value": 5}}},
                             outputs={"export": ["a", 0]})
        workflow = remote.submit(graph)
        last = remote.node("Echo", value=99)
        assert await (await first).fetch() == 41
        assert await (await lifted).fetch() == 42
        result = await workflow
        assert result.ownership == "borrowed" and await result.fetch() == "export"
        assert await (await last).fetch() == 99
        assert len(transport.envelopes) == 1


class SlowArtifactTransport(TraceTransport):
    def __init__(self, runtime):
        super().__init__(runtime)
        self.upload_started = asyncio.Event()
        self.finish_upload = asyncio.Event()

    async def request(self, method, **params):
        if method == "artifact_put":
            self.upload_started.set()
            await self.finish_upload.wait()
        return await super().request(method, **params)


async def test_ready_prefix_executes_while_later_artifact_is_still_uploading():
    runtime = BatchRuntime()
    transport = SlowArtifactTransport(runtime)
    async with Client(transport=transport) as remote:
        first = remote.node("Echo", value=41, return_call=True)
        second = remote.python(lambda value: value + 1, inputs={"value": first.output(0)},
                               return_call=True)
        last = remote.node("Echo", value=second.output(0), return_call=True)
        try:
            await asyncio.wait_for(transport.upload_started.wait(), 1)
            ready = await asyncio.wait_for(first.output(0), 1)
            assert await ready.fetch() == 41
            assert not transport.finish_upload.is_set()
            assert [call_id for call_id, _ in runtime.submissions] == [first.id]
        finally:
            transport.finish_upload.set()
        assert await (await last.output(0)).fetch() == 42
        assert [call_id for call_id, _ in runtime.submissions] == [first.id, second.id, last.id]


async def test_close_accounts_for_an_artifact_call_held_behind_a_ready_prefix():
    runtime = BatchRuntime()
    transport = TraceTransport(runtime)
    original = transport.request
    accepted = asyncio.Event()
    finish_ack = asyncio.Event()

    async def request(method, **params):
        result = await original(method, **params)
        if method == "submit":
            accepted.set()
            await finish_ack.wait()
        return result

    transport.request = request
    remote = Client(transport=transport)
    try:
        first = remote.node("Echo", value=41, return_call=True)
        second = remote.python(lambda value: value + 1, inputs={"value": first.output(0)},
                               return_call=True)
        await asyncio.wait_for(accepted.wait(), 1)
        await remote.close(wait=False)
        await asyncio.wait_for(remote._queue.join(), 1)
        for call in (first, second):
            with pytest.raises(SessionClosed):
                await call
    finally:
        finish_ack.set()
        await remote.close(wait=False)
